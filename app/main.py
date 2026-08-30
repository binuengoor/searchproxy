from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.responses import RedirectResponse

import app.config as _config_module
from app.clean_executor import init_executor, shutdown_executor
import app.clients as _clients_module
from app.observability import init_store, ObservabilityStore
from app.middleware import request_logger as _request_logger_module
from app.middleware.auth import AuthMiddleware, EXCLUDED_PATHS
from app.middleware.mcp_unwrap import MCPBodyUnwrapMiddleware
from app.middleware.metrics import MetricsMiddleware
from app.middleware.correlation import CorrelationIdMiddleware
from app.middleware.json_formatter import JsonFormatter, CorrelationIdFilter
from app.openapi_deref import dereference
from app.services.metrics import get_collector


class HealthResponse(BaseModel):
    """Liveness probe response."""

    status: str = "ok"


log = logging.getLogger(__name__)


async def _purge_loop(store: ObservabilityStore) -> None:
    """Background task: purge old observability records every 6 hours."""
    while True:
        try:
            await store.purge_old()
        except Exception as exc:
            log.warning("Observability purge failed: %s", exc)
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage httpx client lifecycle across startup/shutdown."""
    # --- Logging setup ---
    log_level = getattr(logging, _config_module.settings.LOG_LEVEL.upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove default handlers added by basicConfig in previous runs or by third-party code
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler()
    if _config_module.settings.LOG_FORMAT.lower() == "json":
        handler.setFormatter(JsonFormatter())
        # Attach correlation ID filter so all log records include it
        handler.addFilter(CorrelationIdFilter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root_logger.addHandler(handler)

    log.info("Starting searchproxy (log_format=%s)", _config_module.settings.LOG_FORMAT)
    _clients_module._client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0),  # fallback; all services override with their own timeouts
        follow_redirects=True,
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        http2=True,
    )
    # --- Dedicated thread pool for content cleaning ---
    init_executor(max_workers=16)
    # --- Observability ---
    from app.routers.logs import router as logs_router  # avoid circular import
    app.include_router(logs_router)
    _store = init_store(_config_module.settings)
    _purge_task: asyncio.Task | None = None
    if _config_module.settings.OBSERVABILITY_ENABLED:
        log.info("Observability enabled (retention=%sd)", _config_module.settings.OBSERVABILITY_RETENTION_DAYS)
        _request_logger_module._store = _store
        _request_logger_module._settings = _config_module.settings
        # Run one purge immediately on startup, then start background loop
        try:
            await _store.purge_old()
        except Exception as exc:
            log.warning("Initial observability purge failed: %s", exc)
        _purge_task = asyncio.create_task(_purge_loop(_store))

    yield

    log.info("Shutting down searchproxy")
    shutdown_executor()
    if _purge_task is not None:
        _purge_task.cancel()
        try:
            await _purge_task
        except asyncio.CancelledError:
            pass
    if _clients_module._client is not None:
        await _clients_module._client.aclose()
        _clients_module._client = None


app = FastAPI(
    title="searchproxy",
    description=(
        "Self-hosted search gateway for AI agents.\n\n"
        "Two primary search tools:\n\n"
        "**/v1/retrieve** — Search → rerank → fetch → synthesize. Returns a cited "
        "answer with inline [N] citations and source URLs. Use as the default search tool "
        "for any question requiring web-sourced information (5–15s).\n\n"
        "**/fetch** — Read a specific URL. Returns full markdown content via a tiered "
        "fetch chain (Crawl4AI → Jina → anti-bot). Use when the user provides a URL.\n\n"
        "Additional endpoints (/compat/perplexity, /compat/searxng, /compat/firecrawl) "
        "exist for Open WebUI and legacy client compatibility but are hidden from the "
        "OpenAPI spec — agents should use the two primary tools above.\n\n"
        "**/metrics** — Prometheus monitoring. NOT a search tool."
    ),
    version="0.8.3",
    lifespan=lifespan,
)
# Force OpenAPI 3.0.3 for max client compatibility (MCPHub, Open WebUI).
# OpenAPI 3.1 emits `anyOf: [{type: string}, {type: null}]` for Optional
# fields, which many tool clients cannot parse, causing 422 errors.
app.openapi_version = "3.0.3"

# Overwrite /openapi.json handler so MCPHub receives a $ref-free spec.
_original_openapi = app.openapi


def _dereferenced_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    raw = _original_openapi()
    derefed = dereference(raw)
    app.openapi_schema = derefed
    return derefed


app.openapi = _dereferenced_openapi  # type: ignore[method-assign]

# Register middleware stack (outermost to innermost):
# 1. Correlation ID (outermost)
app.add_middleware(CorrelationIdMiddleware)
# 2. Observability request logging
app.add_middleware(_request_logger_module.ObservabilityMiddleware)
# 3. Metrics request counting
app.add_middleware(MetricsMiddleware)
# 4. Auth token verification
app.add_middleware(AuthMiddleware)
# 5. MCP request body unwrap (innermost)
app.add_middleware(MCPBodyUnwrapMiddleware)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["health"], response_model=HealthResponse, operation_id="health")
async def health() -> HealthResponse:
    """Liveness probe. No auth required."""
    return HealthResponse(status="ok")


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Redirect root to Swagger UI for quick browser testing."""
    return RedirectResponse(url="/docs", status_code=307)


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

from app.routers import search, searxng, vane, fetch, firecrawl, metrics, retrieve

app.include_router(search.router)
app.include_router(searxng.router)
app.include_router(vane.router)
app.include_router(fetch.router)
app.include_router(firecrawl.router)
app.include_router(metrics.router)
app.include_router(retrieve.router)
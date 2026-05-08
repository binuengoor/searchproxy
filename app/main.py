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
from app.observability import init_store, ObservabilityStore
from app.middleware import request_logger as _request_logger_module
from app.middleware.correlation import CorrelationIdMiddleware
from app.middleware.json_formatter import JsonFormatter, CorrelationIdFilter
from app.openapi_deref import dereference
from app.services.metrics import get_collector


class HealthResponse(BaseModel):
    """Liveness probe response."""

    status: str = "ok"


# Module-level shared client, initialized in lifespan
_client: httpx.AsyncClient | None = None

log = logging.getLogger(__name__)


def get_client() -> httpx.AsyncClient:
    """Return the shared httpx client. Raises if called before startup."""
    if _client is None:
        raise RuntimeError("httpx client not initialized")
    return _client


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
    global _client

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
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0),  # fallback; all services override with their own timeouts
        follow_redirects=True,
    )
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
    if _purge_task is not None:
        _purge_task.cancel()
        try:
            await _purge_task
        except asyncio.CancelledError:
            pass
    if _client is not None:
        await _client.aclose()
        _client = None


app = FastAPI(
    title="searchproxy",
    description=(
        "Self-hosted search gateway consolidating multiple search and fetch providers "
        "behind OpenAPI-compatible endpoints.\n\n"
        "Provides four distinct capabilities:\n\n"
        "**/compat/perplexity** — Fast web search for quick factual lookups. Returns "
        "title, URL, and snippet. Use when you need a fast answer to a straightforward question.\n\n"
        "**/v1/retrieve** — Research endpoint: search, rerank, fetch, synthesize. "
        "Returns inline [N] citations and source URLs. Use for sourced answers from multiple web sources (5-15s).\n\n"
        "**/vane** — Deep research with full synthesis. Produces comprehensive reports (60-300s). "
        "Use for complex, analytical research questions.\n\n"
        "**/fetch** — Read a specific URL. Returns full markdown via tiered fetch chain. "
        "Use when the user provides a URL to read or summarize.\n\n"
        "**/metrics** — Infrastructure monitoring (Prometheus format). NOT a search tool."
    ),
    version="0.7.0",
    lifespan=lifespan,
)
# Force OpenAPI 3.0.3 for max client compatibility (MCPHub, Open WebUI).
# OpenAPI 3.1 emits `anyOf: [{type: string}, {type: null}]` for Optional
# fields, which many tool clients cannot parse, causing 422 errors.
app.openapi_version = "3.0.3"

# Overwrite /openapi.json handler so MCPHub receives a $ref-free spec.
_original_openapi = app.openapi


def _dereferenced_openapi() -> dict[str, Any]:
    raw = _original_openapi()
    return dereference(raw)


app.openapi = _dereferenced_openapi  # type: ignore[method-assign]

# Register correlation ID middleware first (outermost) so all downstream
# middleware and route handlers can access the correlation ID.
app.add_middleware(CorrelationIdMiddleware)

# Register observability middleware at import time.
# The middleware uses module-level variables (_store, _settings)
# set later in lifespan; until then it passes through.
app.add_middleware(_request_logger_module.ObservabilityMiddleware)

# ---------------------------------------------------------------------------
# API key middleware
# ---------------------------------------------------------------------------

EXCLUDED_PATHS = {"/health", "/openapi.json", "/docs", "/redoc", "/", "/metrics"}


@app.middleware("http")
async def mcp_body_unwrap(request: Request, call_next: object) -> JSONResponse:
    """Unwrap MCPHub's nested ``body`` key for POST/PUT/PATCH requests.

    When MCPHub generates tools from an OpenAPI spec, it wraps the request
    body inside a ``body`` key: ``{"body": {"query": "..."}}``.
    FastAPI expects the fields at the top level, so this middleware detects
    the wrapper and rewrites the request body to flatten it.
    """
    if request.method in ("POST", "PUT", "PATCH") and request.headers.get(
        "content-type", ""
    ).startswith("application/json"):
        try:
            raw = await request.body()
            if raw:
                data = json.loads(raw)
                # Heuristic: if the body is a dict with exactly one key "body"
                # whose value is also a dict, flatten it.
                if (
                    isinstance(data, dict)
                    and list(data.keys()) == ["body"]
                    and isinstance(data["body"], dict)
                ):
                    log.debug("MCPHub body unwrap: flattening nested 'body' key")
                    # Replace the request body with the unwrapped version
                    new_body = json.dumps(data["body"]).encode()
                    request._body = new_body  # type: ignore[attr-defined]
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass  # Not valid JSON — leave untouched

    return await call_next(request)  # type: ignore[return-value]


@app.middleware("http")
async def auth_middleware(request: Request, call_next: object) -> JSONResponse:
    """Require Bearer token on all routes if SEARCHPROXY_REQUIRE_AUTH is enabled."""
    if not _config_module.settings.SEARCHPROXY_REQUIRE_AUTH:
        return await call_next(request)  # type: ignore[return-value]

    if request.url.path in EXCLUDED_PATHS:
        return await call_next(request)  # type: ignore[return-value]

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid Authorization header"},
        )

    token = auth_header[7:]
    if token != _config_module.settings.SEARCHPROXY_API_KEY:
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid API key"},
        )

    return await call_next(request)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Metrics request counting
# ---------------------------------------------------------------------------

_metrics = get_collector()

_METRICS_EXCLUDED = EXCLUDED_PATHS | {"/metrics"}


@app.middleware("http")
async def metrics_middleware(request: Request, call_next: object) -> JSONResponse:
    """Count every non-excluded request for /metrics endpoint."""
    response = await call_next(request)
    path = request.url.path
    if path not in _METRICS_EXCLUDED:
        _metrics.inc_requests(request.method, path, response.status_code)
    return response


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
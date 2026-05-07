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

from app.config import settings
from app.observability import init_store, ObservabilityStore
from app.middleware import request_logger as _request_logger_module
from app.openapi_deref import dereference


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
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info("Starting searchproxy")
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0),  # fallback; all services override with their own timeouts
        follow_redirects=True,
    )
    # --- Observability ---
    from app.routers.logs import router as logs_router  # avoid circular import
    app.include_router(logs_router)
    _store = init_store(settings)
    _purge_task: asyncio.Task | None = None
    if settings.OBSERVABILITY_ENABLED:
        log.info("Observability enabled (retention=%sd)", settings.OBSERVABILITY_RETENTION_DAYS)
        _request_logger_module._store = _store
        _request_logger_module._settings = settings
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
    description="Self-hosted web search and content fetch gateway",
    version="0.1.0",
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

# Register observability middleware at import time.
# The middleware uses module-level variables (_store, _settings)
# set later in lifespan; until then it passes through.
app.add_middleware(_request_logger_module.ObservabilityMiddleware)

# ---------------------------------------------------------------------------
# API key middleware
# ---------------------------------------------------------------------------

EXCLUDED_PATHS = {"/health", "/openapi.json", "/docs", "/redoc", "/"}


@app.middleware("http")
async def mcp_body_unwrap(request: Request, call_next: object) -> JSONResponse:
    """Unwrap MCPHub's nested ``body`` key for POST/PUT/PATCH requests.

    When MCPHub generates tools from an OpenAPI spec, it wraps the request
    body model inside a ``body`` key: ``{"body": {"query": "..."}}``.
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
    if not settings.SEARCHPROXY_REQUIRE_AUTH:
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
    if token != settings.SEARCHPROXY_API_KEY:
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid API key"},
        )

    return await call_next(request)  # type: ignore[return-value]


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

from app.routers import search, searxng, vane, fetch, firecrawl

app.include_router(search.router)
app.include_router(searxng.router)
app.include_router(vane.router)
app.include_router(fetch.router)
app.include_router(firecrawl.router)

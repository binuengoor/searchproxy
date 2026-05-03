from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.responses import RedirectResponse

from app.config import settings

# Module-level shared client, initialized in lifespan
_client: httpx.AsyncClient | None = None

log = logging.getLogger(__name__)


def get_client() -> httpx.AsyncClient:
    """Return the shared httpx client. Raises if called before startup."""
    if _client is None:
        raise RuntimeError("httpx client not initialized")
    return _client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage httpx client lifecycle across startup/shutdown."""
    global _client
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info("Starting searchproxy")
    _client = httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=True)
    yield
    log.info("Shutting down searchproxy")
    if _client is not None:
        await _client.aclose()
        _client = None


app = FastAPI(
    title="searchproxy",
    description="Self-hosted web search and content fetch gateway",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# API key middleware
# ---------------------------------------------------------------------------

EXCLUDED_PATHS = {"/health", "/openapi.json", "/docs", "/redoc", "/"}


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

@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Liveness probe. No auth required."""
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Redirect root to Swagger UI for quick browser testing."""
    return RedirectResponse(url="/docs", status_code=307)


# ---------------------------------------------------------------------------
# Routers (stubbed — implemented by subagents 2/3/4)
# ---------------------------------------------------------------------------

# These imports will resolve once subagents add their router modules.
# The routers are registered here so the app object is complete.
try:
    from app.routers import search, searxng, vane, fetch
    app.include_router(search.router)
    app.include_router(searxng.router)
    app.include_router(vane.router)
    app.include_router(fetch.router)
except ImportError:
    # Routers not yet implemented — app still starts for basic tests
    pass

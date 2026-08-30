"""Authentication middleware for Bearer token validation."""
from __future__ import annotations

import logging
from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import app.config as _config_module

log = logging.getLogger(__name__)

EXCLUDED_PATHS = frozenset({"/health", "/openapi.json", "/docs", "/redoc", "/", "/metrics"})


class AuthMiddleware(BaseHTTPMiddleware):
    """Require Bearer token on all non-excluded routes if SEARCHPROXY_REQUIRE_AUTH is enabled."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Response]) -> Response:
        if not _config_module.settings.SEARCHPROXY_REQUIRE_AUTH:
            return await call_next(request)

        if request.url.path in EXCLUDED_PATHS:
            return await call_next(request)

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

        return await call_next(request)

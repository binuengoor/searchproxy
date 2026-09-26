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

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Response]
    ) -> Response:
        if not _config_module.settings.SEARCHPROXY_REQUIRE_AUTH:
            return await call_next(request)

        if request.url.path in EXCLUDED_PATHS:
            return await call_next(request)

        # Allow authenticated MCP session message POSTs
        if request.url.path in ("/messages", "/messages/", "/sse/messages", "/sse/messages/"):
            session_id = request.query_params.get("session_id")
            if session_id:
                from app.mcp_server import is_valid_mcp_session

                if is_valid_mcp_session(session_id):
                    return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        elif "api_key" in request.query_params:
            token = request.query_params["api_key"]
        elif "token" in request.query_params:
            token = request.query_params["token"]

        if not token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        if token != _config_module.settings.SEARCHPROXY_API_KEY:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid API key"},
            )

        return await call_next(request)

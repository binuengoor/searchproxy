"""Correlation ID middleware — X-Correlation-ID header propagation.

Reads the X-Correlation-ID request header. If present, uses it;
if absent, generates a short UUID. Stores the ID on request.state.correlation_id
so downstream services and log formatters can access it.

Also sets a context variable (_current_correlation_id) for structured logging.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from starlette.types import ASGIApp, Receive, Scope, Send

# Context variable for structured logging — set per-request, cleared on exit
_current_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


class CorrelationIdMiddleware:
    """ASGI middleware that ensures every request has a correlation ID.

    - If X-Correlation-ID header is present, uses that value.
    - Otherwise, generates a short UUID (8 chars) for traceability.
    - Stores the value on scope["state"]["correlation_id"] for downstream access.
    - Sets the _current_correlation_id context variable for log filters.
    - Adds X-Correlation-ID to response headers.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(
            (k.decode().lower(), v.decode())
            for k, v in scope.get("headers", [])
        )

        correlation_id = headers.get("x-correlation-id", "") or uuid.uuid4().hex[:8]
        scope.setdefault("state", {})["correlation_id"] = correlation_id

        # Set context variable for structured logging
        token = _current_correlation_id.set(correlation_id)
        try:
            async def _send(message: dict) -> None:
                if message["type"] == "http.response.start":
                    resp_headers = list(message.get("headers", []))
                    resp_headers.append((b"x-correlation-id", correlation_id.encode()))
                    message["headers"] = resp_headers
                await send(message)

            await self.app(scope, receive, _send)
        finally:
            _current_correlation_id.reset(token)


def get_correlation_id(scope: dict) -> str:
    """Extract correlation_id from ASGI scope state."""
    return scope.get("state", {}).get("correlation_id", "")
"""Pure ASGI request/response capture middleware.

Intercepts http.response.start and http.response.body ASGI messages directly,
so the full response body is available before Starlette wraps it in any proxy.

Skips response body capture for streaming responses (text/event-stream) to
avoid buffering large SSE streams in memory.
"""
from __future__ import annotations

import json as _json
import logging
import time
import uuid
from typing import Any

from app.config import Settings
from app.middleware.correlation import _current_correlation_id
from app.observability import LogRecord, ObservabilityStore

log = logging.getLogger(__name__)

_EXCLUDED_PATHS = {
    "/health",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/",
    "/logs",
    "/api/logs",
}

# Content types that indicate streaming — skip body capture to save memory
_STREAMING_CONTENT_TYPES = frozenset({
    "text/event-stream",
    "text/plain; charset=utf-8",  # Vane streaming
})


def _derive_source(path: str, response_body: str) -> str:
    if path in ("/fetch", "/compat/firecrawl/scrape"):
        try:
            data = _json.loads(response_body)
            return data.get("source", "")
        except Exception:
            return ""
    if path in ("/compat/perplexity", "/v1/search"):
        return "litellm"
    if path == "/vane":
        return "vane"
    if path in ("/compat/searxng", "/compat/searxng/search"):
        return "searxng"
    return ""


# Module-level store/settings; set during lifespan.
_store: ObservabilityStore | None = None
_settings: Settings | None = None


class ObservabilityMiddleware:
    """ASGI middleware that captures request/response metadata.

    Must NOT inherit from BaseHTTPMiddleware — that class consumes responses
    into a streaming representation where .body is inaccessible.
    """

    def __init__(
        self,
        app: Any,
        store: ObservabilityStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.app = app
        self._store = store
        self._settings = settings

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        store = self._store if self._store is not None else _store
        settings = self._settings if self._settings is not None else _settings
        if not settings or not settings.OBSERVABILITY_ENABLED or store is None:
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())[:8]
        path = scope.get("path", "")
        method = scope.get("method", "")
        start = time.perf_counter()

        # --- Request body capture ---
        request_body = ""
        request_headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}

        if path not in _EXCLUDED_PATHS and method in ("POST", "PUT", "PATCH"):
            # Intercept receive to capture body
            body_parts: list[bytes] = []
            original_receive = receive

            async def _receive() -> dict:
                msg = await original_receive()
                if msg.get("type") == "http.request":
                    body_parts.append(msg.get("body", b""))
                return msg

            # Run app with intercepted receive
            await self._run_app(scope, _receive, send, store, settings, request_id, path, method, start, request_headers, body_parts)
        else:
            # No body capture needed
            await self._run_app(scope, receive, send, store, settings, request_id, path, method, start, request_headers, None)

    async def _run_app(
        self,
        scope: dict,
        receive: Any,
        send: Any,
        store: ObservabilityStore,
        settings: Settings,
        request_id: str,
        path: str,
        method: str,
        start: float,
        request_headers: dict,
        body_parts: list | None,
    ) -> None:
        status_code = 0
        response_headers: dict = {}
        response_body_parts: list[bytes] = []
        is_streaming = False

        original_send = send

        async def _send(msg: dict) -> None:
            nonlocal status_code, response_headers, is_streaming
            if msg["type"] == "http.response.start":
                status_code = msg.get("status", 0)
                # Check if this is a streaming response
                resp_ct = ""
                for k, v in msg.get("headers", []):
                    if k.decode().lower() == "content-type":
                        resp_ct = v.decode().lower()
                is_streaming = resp_ct in _STREAMING_CONTENT_TYPES
                response_headers = {k.decode(): v.decode() for k, v in msg.get("headers", [])}
            elif msg["type"] == "http.response.body":
                # Only buffer response body for non-streaming responses
                if not is_streaming:
                    response_body_parts.append(msg.get("body", b""))
            await original_send(msg)

        try:
            await self.app(scope, receive, _send)
        except Exception:
            raise
        finally:
            if path not in _EXCLUDED_PATHS:
                elapsed_ms = (time.perf_counter() - start) * 1000

                # Assemble request body
                req_body = ""
                if body_parts is not None:
                    full_body = b"".join(body_parts)
                    req_body = full_body[:8_192].decode("utf-8", errors="replace")

                # Assemble response body (skip for streaming to save memory)
                if is_streaming:
                    resp_body = "[streaming response — body not captured]"
                else:
                    full_resp_body = b"".join(response_body_parts)
                    resp_body = full_resp_body[:8_192].decode("utf-8", errors="replace") if full_resp_body else ""
                    if not resp_body:
                        resp_body = "[body not captured]"

                source = _derive_source(path, resp_body if not is_streaming else "")

                # Client IP
                client = scope.get("client")
                client_ip = client[0] if client else ""

                try:
                    await store.insert(
                        LogRecord(
                            request_id=request_id,
                            method=method,
                            path=path,
                            query_params=scope.get("query_string", b"").decode("utf-8", errors="replace"),
                            status_code=status_code or None,
                            response_time_ms=elapsed_ms,
                            client_ip=client_ip,
                            user_agent=request_headers.get("user-agent"),
                            request_headers=request_headers,
                            request_body=req_body,
                            response_headers=response_headers,
                            response_body=resp_body,
                            error=None,
                            source=source,
                            correlation_id=_current_correlation_id.get(""),
                        )
                    )
                except Exception as exc:
                    log.warning("Observability insert failed: %s", exc)

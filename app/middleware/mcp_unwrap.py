"""MCPHub & Open WebUI request body unwrap middleware."""
from __future__ import annotations

import json
import logging
from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger(__name__)


class MCPBodyUnwrapMiddleware(BaseHTTPMiddleware):
    """Unwrap MCPHub's nested ``body`` key for POST/PUT/PATCH requests.

    When MCPHub or Open WebUI generates tools from an OpenAPI spec, it may wrap the
    request body inside a ``body`` key: ``{"body": {"query": "..."}}`` or ``{"body": "{\"query\": \"...\"}"}``.
    This middleware detects the wrapper and rewrites the request body to flatten it.
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Response]) -> Response:
        if request.method in ("POST", "PUT", "PATCH") and request.headers.get(
            "content-type", ""
        ).startswith("application/json"):
            try:
                raw = await request.body()
                if raw:
                    data = json.loads(raw)
                    if isinstance(data, dict) and list(data.keys()) == ["body"]:
                        nested = data["body"]
                        if isinstance(nested, str):
                            try:
                                nested = json.loads(nested)
                            except Exception:
                                pass
                        if isinstance(nested, dict):
                            log.debug("MCPHub body unwrap: flattening nested 'body' key")
                            new_body = json.dumps(nested).encode()
                            request._body = new_body  # type: ignore[attr-defined]
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass  # Not valid JSON — leave untouched

        return await call_next(request)

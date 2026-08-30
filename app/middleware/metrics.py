"""Metrics recording middleware."""
from __future__ import annotations

from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.services.metrics import get_collector
from app.middleware.auth import EXCLUDED_PATHS

_METRICS_EXCLUDED = EXCLUDED_PATHS | {"/metrics"}


class MetricsMiddleware(BaseHTTPMiddleware):
    """Count every non-excluded request for the /metrics endpoint."""

    def __init__(self, app: object) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._metrics = get_collector()

    async def dispatch(self, request: Request, call_next: Callable[[Request], Response]) -> Response:
        response = await call_next(request)
        path = request.url.path
        if path not in _METRICS_EXCLUDED:
            self._metrics.inc_requests(request.method, path, response.status_code)
        return response

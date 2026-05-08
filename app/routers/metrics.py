"""Prometheus-style /metrics endpoint."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import PlainTextResponse

from app.services.metrics import get_collector

router = APIRouter(tags=["metrics"])


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    operation_id="metrics",
    summary="Get request and fetch-chain metrics",
    description="Returns Prometheus-format counters for HTTP requests and fetch chain tier outcomes. No auth required.",
)
async def get_metrics() -> PlainTextResponse:
    """Prometheus-style metrics endpoint. No auth required."""
    return PlainTextResponse(
        get_collector().format_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
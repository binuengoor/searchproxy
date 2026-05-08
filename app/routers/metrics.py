"""Prometheus-style /metrics endpoint."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import PlainTextResponse

from app.services.metrics import get_collector

router = APIRouter(tags=["metrics"])

METRICS_DESCRIPTION = """\
Monitoring endpoint that returns Prometheus-format counters.

**Not a search tool** — this is for infrastructure monitoring. Returns two 
metric families:

- `searchproxy_requests_total{method, endpoint, status}` — HTTP request counts
- `searchproxy_fetch_chain_tiers_total{tier, outcome}` — fetch chain attempts 
  per tier (crawl4ai, jina, scrape_do, scraperapi) and outcome (success, fail)

No authentication required. Format is scrape-compatible with Prometheus and 
Grafana. Do NOT use this endpoint for search or content retrieval.
"""

@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    operation_id="metrics",
    summary="Infrastructure metrics (Prometheus format)",
    description=METRICS_DESCRIPTION,
)
async def get_metrics() -> PlainTextResponse:
    """Prometheus-style metrics endpoint. No auth required."""
    return PlainTextResponse(
        get_collector().format_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )

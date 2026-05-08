"""In-memory metrics collector for Prometheus-style /metrics endpoint.

Thread-safe for single-process async — no external dependencies.
"""

from __future__ import annotations

import threading
from typing import Any


class MetricsCollector:
    """Collects request counts and fetch-chain tier metrics in memory.

    All methods are safe for concurrent use from multiple threads
    (GIL + thread lock). FastAPI async handlers don't strictly need
    the lock, but it's cheap insurance if someone adds background workers.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request_counts: dict[str, int] = {}
        self._tier_counts: dict[str, int] = {}

    def inc_requests(self, method: str, endpoint: str, status: int) -> None:
        key = f"{method.upper()}:{endpoint}:{status}"
        with self._lock:
            self._request_counts[key] = self._request_counts.get(key, 0) + 1

    def inc_tier(self, tier: str, outcome: str) -> None:
        key = f"{tier}:{outcome}"
        with self._lock:
            self._tier_counts[key] = self._tier_counts.get(key, 0) + 1

    def format_prometheus(self) -> str:
        """Render metrics in Prometheus exposition format."""
        lines: list[str] = []

        # Request counters
        lines.append("# HELP searchproxy_requests_total Total HTTP requests")
        lines.append("# TYPE searchproxy_requests_total counter")
        with self._lock:
            sorted_requests = sorted(self._request_counts.items())
        for key, count in sorted_requests:
            method, endpoint, status = key.split(":", 2)
            lines.append(
                f'searchproxy_requests_total{{method="{method}",endpoint="{endpoint}",status="{status}"}} {count}'
            )

        lines.append("")
        lines.append("# HELP searchproxy_fetch_chain_tiers_total Fetch chain tier attempts")
        lines.append("# TYPE searchproxy_fetch_chain_tiers_total counter")
        with self._lock:
            sorted_tiers = sorted(self._tier_counts.items())
        for key, count in sorted_tiers:
            tier, outcome = key.split(":", 1)
            lines.append(
                f'searchproxy_fetch_chain_tiers_total{{tier="{tier}",outcome="{outcome}"}} {count}'
            )

        lines.append("")
        return "\n".join(lines)


# Module-level singleton
_collector: MetricsCollector = MetricsCollector()


def get_collector() -> MetricsCollector:
    """Return the module-level metrics collector singleton."""
    return _collector
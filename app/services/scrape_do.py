"""Scrape.do service client — anti-bot firebreak, quarantined credits."""

from __future__ import annotations

import logging
from datetime import datetime

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.services.crawl4ai import FetchResult

log = logging.getLogger(__name__)

# Module-level credit tracker: persists for process lifetime
_credit_tracker: dict[str, object] = {
    "scrape_do": {"used": 0, "limit": 1000, "month": 0},
}


def _check_credits(service: str, used: int, limit: int, month: int) -> bool:
    """Return True if credits are available (not exhausted and within same month)."""
    now = datetime.utcnow()
    current_month = now.month + now.year * 12
    return used < limit and month == current_month


class ScrapeDoClient:
    """Standalone async client for the Scrape.do anti-bot service.

    Does not reach into other services. Owns its own request logic.
    Monthly credit counter resets on calendar-month boundaries.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def fetch(self, url: str) -> FetchResult:
        """GET https://api.scrape.do/?token={SCRAPE_DO_API_KEY}&url={url}.

        Graceful degradation: on any error (timeout, HTTP error, parse failure,
        credit exhaustion) returns ``FetchResult(success=False, ...)`` so callers
        always get a valid result and never need to handle exceptions.

        Credit tracking: 1,000/month. Counter resets on calendar month boundary.
        If credits are exhausted, returns immediately without making an HTTP call.

        Args:
            url: The target URL to fetch.

        Returns:
            FetchResult with raw HTML content on success, or success=False on failure.
        """
        if not self._settings.SCRAPE_DO_API_KEY:
            log.info("ScrapeDo fetch skipped: SCRAPE_DO_API_KEY not set")
            return FetchResult(
                success=False,
                url=url,
                error="SCRAPE_DO_API_KEY not configured",
                source="scrape_do",
            )

        now = datetime.utcnow()
        current_month = now.month + now.year * 12

        tracker = _credit_tracker["scrape_do"]  # type: dict[str, object]
        stored_month: int = tracker["month"]  # type: ignore

        # Reset counter on new month
        if stored_month != current_month:
            tracker["used"] = 0
            tracker["month"] = current_month
            log.info("ScrapeDo credit counter reset for new month")

        used: int = tracker["used"]  # type: ignore
        limit: int = tracker["limit"]  # type: ignore

        if not _check_credits("scrape_do", used, limit, current_month):
            log.warning("ScrapeDo credit limit reached (%d/%d) — refusing request", used, limit)
            return FetchResult(
                success=False,
                url=url,
                error="credit limit reached",
                source="scrape_do",
            )

        log.info("ScrapeDo fetch: %s", url)

        timeout = httpx.Timeout(
            timeout=float(self._settings.FETCH_TIMEOUT),
            connect=5.0,
        )

        scrape_url = (
            f"https://api.scrape.do/"
            f"?token={self._settings.SCRAPE_DO_API_KEY}"
            f"&url={url}"
        )

        try:
            response = await self._client.get(
                scrape_url,
                timeout=timeout,
            )
            status_code = response.status_code
            response.raise_for_status()
            # Scrape.do returns raw page HTML
            html = response.text
        except httpx.TimeoutException:
            log.warning("ScrapeDo fetch timed out for %s", url)
            return FetchResult(
                success=False,
                url=url,
                error="timeout",
                source="scrape_do",
            )
        except httpx.HTTPStatusError as exc:
            log.warning(
                "ScrapeDo fetch returned HTTP %d for %s",
                exc.response.status_code,
                url,
            )
            return FetchResult(
                success=False,
                url=url,
                error=f"HTTP {exc.response.status_code}",
                status_code=exc.response.status_code,
                source="scrape_do",
            )
        except Exception as exc:
            log.warning("ScrapeDo fetch failed for %s: %s", url, exc)
            return FetchResult(
                success=False,
                url=url,
                error=str(exc),
                source="scrape_do",
            )

        # Increment credit counter on success
        tracker["used"] = used + 1
        log.info(
            "ScrapeDo fetch succeeded for %s — credits used: %d/%d",
            url,
            tracker["used"],
            limit,
        )

        return FetchResult(
            success=True,
            url=url,
            markdown=html,  # Raw HTML — markdown conversion can be added later
            status_code=status_code,
            source="scrape_do",
        )

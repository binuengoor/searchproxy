"""ScraperAPI service client — anti-bot firebreak, quarantined credits."""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import quote

import httpx

from app.config import Settings
from app.services.crawl4ai import FetchResult

log = logging.getLogger(__name__)

# Module-level credit tracker: persists for process lifetime
_credit_tracker: dict[str, object] = {
    "scraperapi": {"used": 0, "limit": 1000, "month": 0},
}


def _check_credits(service: str, used: int, limit: int, month: int) -> bool:
    """Return True if credits are available (not exhausted and within same month)."""
    now = datetime.utcnow()
    current_month = now.month + now.year * 12
    return used < limit and month == current_month


class ScraperAPIClient:
    """Standalone async client for the ScraperAPI anti-bot service.

    Does not reach into other services. Owns its own request logic.
    Monthly credit counter resets on calendar-month boundaries.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def fetch(self, url: str) -> FetchResult:
        """GET https://api.scraperapi.com/?api_key={SCRAPERAPI_API_KEY}&url={url}.

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
        if not self._settings.SCRAPERAPI_API_KEY:
            log.info("ScraperAPI fetch skipped: SCRAPERAPI_API_KEY not set")
            return FetchResult(
                success=False,
                url=url,
                error="SCRAPERAPI_API_KEY not configured",
                source="scraperapi",
            )

        now = datetime.utcnow()
        current_month = now.month + now.year * 12

        tracker = _credit_tracker["scraperapi"]  # type: dict[str, object]
        stored_month: int = tracker["month"]  # type: ignore

        # Reset counter on new month
        if stored_month != current_month:
            tracker["used"] = 0
            tracker["month"] = current_month
            log.info("ScraperAPI credit counter reset for new month")

        used: int = tracker["used"]  # type: ignore
        limit: int = tracker["limit"]  # type: ignore

        if not _check_credits("scraperapi", used, limit, current_month):
            log.warning(
                "ScraperAPI credit limit reached (%d/%d) — refusing request",
                used,
                limit,
            )
            return FetchResult(
                success=False,
                url=url,
                error="credit limit reached",
                source="scraperapi",
            )

        log.info("ScraperAPI fetch: %s", url)

        timeout = httpx.Timeout(
            timeout=float(self._settings.FETCH_TIMEOUT),
            connect=5.0,
        )

        encoded_url = quote(url, safe="")
        scrape_url = (
            f"https://api.scraperapi.com/"
            f"?api_key={self._settings.SCRAPERAPI_API_KEY}"
            f"&url={encoded_url}"
        )

        try:
            response = await self._client.get(
                scrape_url,
                timeout=timeout,
            )
            status_code = response.status_code
            response.raise_for_status()
            # ScraperAPI returns raw page HTML
            html = response.text
        except httpx.TimeoutException:
            log.warning("ScraperAPI fetch timed out for %s", url)
            return FetchResult(
                success=False,
                url=url,
                error="timeout",
                source="scraperapi",
            )
        except httpx.HTTPStatusError as exc:
            log.warning(
                "ScraperAPI fetch returned HTTP %d for %s",
                exc.response.status_code,
                url,
            )
            return FetchResult(
                success=False,
                url=url,
                error=f"HTTP {exc.response.status_code}",
                status_code=exc.response.status_code,
                source="scraperapi",
            )
        except Exception as exc:
            log.warning("ScraperAPI fetch failed for %s: %s", url, exc)
            return FetchResult(
                success=False,
                url=url,
                error=str(exc),
                source="scraperapi",
            )

        # Increment credit counter on success
        tracker["used"] = used + 1
        log.info(
            "ScraperAPI fetch succeeded for %s — credits used: %d/%d",
            url,
            tracker["used"],
            limit,
        )

        return FetchResult(
            success=True,
            url=url,
            markdown=html,  # Raw HTML — markdown conversion can be added later
            status_code=status_code,
            source="scraperapi",
        )

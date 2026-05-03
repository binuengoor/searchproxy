"""Base class for anti-bot firebreak services with monthly credit tracking."""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import quote

import httpx

from app.config import Settings
from app.services.crawl4ai import FetchResult

log = logging.getLogger(__name__)


def _current_month_key() -> int:
    """Return a comparable integer for the current calendar month.

    ``year * 12 + (month - 1)`` preserves ordering across year boundaries.
    """
    now = datetime.utcnow()
    return now.year * 12 + (now.month - 1)


def _check_credits(used: int, limit: int, month: int) -> bool:
    """Return True if credits are available (not exhausted and within same month)."""
    return used < limit and month == _current_month_key()


class AntiBotClient:
    """Base class for anti-bot fetch services with monthly credit tracking.

    Subclasses set ``_SERVICE_NAME``, ``_API_URL_TEMPLATE``, ``_SOURCE``,
    and ``_DEFAULT_CREDIT_LIMIT``, then ``fetch()`` works automatically.

    Graceful degradation: on any error (timeout, HTTP error, credit exhaustion)
    returns ``FetchResult(success=False, ...)`` so callers always get a valid
    result and never need to handle exceptions.
    """

    _SERVICE_NAME: str = ""       # e.g. "scrape_do", "scraperapi"
    _API_URL_TEMPLATE: str = ""  # e.g. "https://api.scrape.do/?token={key}&url={url}"
    _SOURCE: str = ""            # e.g. "scrape_do", "scraperapi"
    _DEFAULT_CREDIT_LIMIT: int = 1000

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._timeout = httpx.Timeout(
            timeout=float(settings.FETCH_TIMEOUT),
            connect=5.0,
        )
        self._tracker: dict[str, int] = {
            "used": 0,
            "limit": self._DEFAULT_CREDIT_LIMIT,
            "month": 0,
        }

    def _api_key(self) -> str:
        """Return the API key for this service. Subclasses must override."""
        raise NotImplementedError

    def _build_url(self, target_url: str) -> str:
        """Build the full API URL from the template, key, and encoded target."""
        encoded = quote(target_url, safe="")
        return self._API_URL_TEMPLATE.format(key=self._api_key(), url=encoded)

    async def fetch(self, url: str) -> FetchResult:
        """Fetch a URL through the anti-bot service.

        Credit tracking: monthly limit. Counter resets on calendar month boundary.
        If credits are exhausted, returns immediately without making an HTTP call.
        """
        api_key = self._api_key()
        if not api_key:
            log.info("%s fetch skipped: API key not set", self._SERVICE_NAME)
            return FetchResult(
                success=False,
                url=url,
                error=f"{self._SERVICE_NAME} API key not configured",
                source=self._SOURCE,
            )

        current_month = _current_month_key()
        stored_month: int = self._tracker["month"]

        # Reset counter on new month
        if stored_month != current_month:
            self._tracker["used"] = 0
            self._tracker["month"] = current_month
            log.info("%s credit counter reset for new month", self._SERVICE_NAME)

        used: int = self._tracker["used"]
        limit: int = self._tracker["limit"]

        if not _check_credits(used, limit, current_month):
            log.warning(
                "%s credit limit reached (%d/%d) — refusing request",
                self._SERVICE_NAME,
                used,
                limit,
            )
            return FetchResult(
                success=False,
                url=url,
                error="credit limit reached",
                source=self._SOURCE,
            )

        log.info("%s fetch: %s", self._SERVICE_NAME, url)
        scrape_url = self._build_url(url)

        try:
            response = await self._client.get(
                scrape_url,
                timeout=self._timeout,
            )
            status_code = response.status_code
            response.raise_for_status()
            html = response.text
        except httpx.TimeoutException:
            log.warning("%s fetch timed out for %s", self._SERVICE_NAME, url)
            return FetchResult(
                success=False,
                url=url,
                error="timeout",
                source=self._SOURCE,
            )
        except httpx.HTTPStatusError as exc:
            log.warning(
                "%s fetch returned HTTP %d for %s",
                self._SERVICE_NAME,
                exc.response.status_code,
                url,
            )
            return FetchResult(
                success=False,
                url=url,
                error=f"HTTP {exc.response.status_code}",
                status_code=exc.response.status_code,
                source=self._SOURCE,
            )
        except Exception as exc:
            log.warning("%s fetch failed for %s: %s", self._SERVICE_NAME, url, exc)
            return FetchResult(
                success=False,
                url=url,
                error=str(exc),
                source=self._SOURCE,
            )

        # Increment credit counter on success
        self._tracker["used"] = used + 1
        log.info(
            "%s fetch succeeded for %s — credits used: %d/%d",
            self._SERVICE_NAME,
            url,
            self._tracker["used"],
            limit,
        )

        return FetchResult(
            success=True,
            url=url,
            markdown=html,  # Raw HTML — markdown conversion can be added later
            status_code=status_code,
            source=self._SOURCE,
        )
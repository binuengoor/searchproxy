"""Base class for anti-bot firebreak services with monthly credit tracking."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from app.config import Settings
from app.services.fetch_utils import safe_fetch
from app.services.models import FetchResult

log = logging.getLogger(__name__)


def _current_month_key() -> int:
    """Return a comparable integer for the current calendar month.

    ``year * 12 + (month - 1)`` preserves ordering across year boundaries.
    """
    now = datetime.now(timezone.utc)
    return now.year * 12 + (now.month - 1)


def _check_credits(used: int, limit: int) -> bool:
    """Return True if credits are still available."""
    return used < limit


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
            timeout=float(settings.ANTIBOT_TIMEOUT),
            connect=self._settings.CONNECT_TIMEOUT,
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

        if not _check_credits(used, limit):
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

        result = await safe_fetch(
            self._client,
            method="GET",
            url=scrape_url,
            source=self._SOURCE,
            timeout=self._timeout,
        )

        # Increment credit counter on success
        if result.success:
            self._tracker["used"] = used + 1
            log.info(
                "%s fetch succeeded for %s — credits used: %d/%d",
                self._SERVICE_NAME,
                url,
                self._tracker["used"],
                limit,
            )

        return result
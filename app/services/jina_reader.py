"""Jina Reader service client — free cloud backup fetcher."""

from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.services.fetch_utils import safe_fetch
from app.services.models import FetchResult

log = logging.getLogger(__name__)


class JinaReaderClient:
    """Standalone async client for the Jina Reader service.

    Does not reach into other services. Owns its own request logic.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._timeout = httpx.Timeout(
            timeout=float(settings.JINA_TIMEOUT),
            connect=self._settings.CONNECT_TIMEOUT,
        )

    async def fetch(self, url: str) -> FetchResult:
        """GET Jina Reader at https://r.jina.ai/{url}.

        Graceful degradation: on any error (timeout, HTTP error, parse failure)
        returns ``FetchResult(success=False, ...)`` so callers always get a valid
        result and never need to handle exceptions.

        Args:
            url: The target URL to fetch.

        Returns:
            FetchResult with markdown content on success, or success=False on failure.
        """
        log.info("Jina Reader fetch: %s", url)

        headers: dict[str, str] = {}
        if self._settings.JINA_API_KEY:
            headers["Authorization"] = f"Bearer {self._settings.JINA_API_KEY}"

        return await safe_fetch(
            self._client,
            method="GET",
            url=f"https://r.jina.ai/{url}",
            source="jina",
            timeout=self._timeout,
            headers=headers,
            check_403=True,
        )
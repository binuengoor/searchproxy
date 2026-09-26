"""Crawl4AI service client — self-hosted primary fetcher."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings
from app.services.fetch_utils import safe_fetch
from app.services.models import FetchResult

log = logging.getLogger(__name__)


class Crawl4AIClient:
    """Standalone async client for the self-hosted Crawl4AI service.

    Does not reach into other services. Owns its own request logic.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._timeout = httpx.Timeout(
            timeout=float(settings.CRAWL4AI_TIMEOUT),
            connect=self._settings.CONNECT_TIMEOUT,
        )

    async def fetch_markdown(
        self,
        url: str,
        content_filter: str | None = None,
        content_query: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        screenshot: bool = False,
    ) -> FetchResult:
        """POST to Crawl4AI /md for plain markdown fetch with optional actions and screenshot.

        Graceful degradation: on any error (timeout, HTTP error, parse failure)
        returns ``FetchResult(success=False, ...)`` so callers always get a valid
        result and never need to handle exceptions.

        Args:
            url: The target URL to fetch.
            content_filter: Crawl4AI filter mode — 'fit' (default), 'bm25', or 'raw'.
            content_query: BM25 query string. Required when content_filter='bm25'.
            actions: Interactive browser actions (click, scroll, wait, etc.).
            screenshot: If True, request headless browser screenshot.

        Returns:
            FetchResult with markdown content on success, or success=False on failure.
        """
        log.info(
            "Crawl4AI fetch_markdown: %s (filter=%s, actions=%s, screenshot=%s)",
            url,
            content_filter or "fit",
            bool(actions),
            screenshot,
        )

        body: dict[str, Any] = {"url": url, "f": content_filter or "fit"}
        if content_filter == "bm25" and content_query:
            body["q"] = content_query
        if actions:
            body["actions"] = actions
        if screenshot:
            body["screenshot"] = True

        headers: dict[str, str] | None = None
        if self._settings.CRAWL4AI_API_TOKEN:
            headers = {"Authorization": f"Bearer {self._settings.CRAWL4AI_API_TOKEN}"}

        return await safe_fetch(
            self._client,
            method="POST",
            url=f"{self._settings.CRAWL4AI_URL}/md",
            source="crawl4ai",
            timeout=self._timeout,
            json_body=body,
            headers=headers,
            check_403=True,
        )

"""SearXNG search provider (local fallback)."""

from __future__ import annotations

import logging
import httpx

from app.services.search.base import BaseSearchProvider
from app.services.search.models import SearchResult

log = logging.getLogger(__name__)


class SearxngSearchProvider(BaseSearchProvider):
    """SearXNG search provider — acts as local fallback and safety net."""

    @property
    def name(self) -> str:
        return "searxng"

    @property
    def tier(self) -> int:
        return 2

    @property
    def is_available(self) -> bool:
        return bool(self._settings.SEARXNG_URL)

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        if not self.is_available or not self._settings.SEARXNG_URL:
            raise ValueError("SearXNG URL not configured")

        url = self._settings.SEARXNG_URL

        params = {
            "q": query,
            "format": "json",
        }

        timeout = httpx.Timeout(
            timeout=float(self._settings.SEARCH_TIMEOUT),
            connect=self._settings.CONNECT_TIMEOUT,
        )

        response = await self._client.get(
            url,
            params=params,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()

        results: list[SearchResult] = []
        raw_items = data.get("results", []) if isinstance(data, dict) else []
        for item in raw_items[:max_results]:
            title = str(item.get("title") or "")
            url = str(item.get("url") or "")
            snippet = str(item.get("content") or "")
            if url:
                results.append(SearchResult(title=title, url=url, snippet=snippet))

        return results

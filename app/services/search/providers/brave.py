"""Brave Search API provider."""

from __future__ import annotations

import logging
import httpx

from app.services.search.base import BaseSearchProvider
from app.services.search.models import SearchResult

log = logging.getLogger(__name__)

_BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"


class BraveSearchProvider(BaseSearchProvider):
    """Brave Search API provider."""

    @property
    def name(self) -> str:
        return "brave"

    @property
    def tier(self) -> int:
        return 1

    @property
    def is_available(self) -> bool:
        return bool(self._settings.BRAVE_API_KEY)

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        if not self.is_available or not self._settings.BRAVE_API_KEY:
            raise ValueError("Brave API key not configured")

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self._settings.BRAVE_API_KEY,
        }
        params: dict[str, str | int] = {
            "q": query,
            "count": min(max_results, 20),
        }

        timeout = httpx.Timeout(
            timeout=float(self._settings.SEARCH_TIMEOUT),
            connect=self._settings.CONNECT_TIMEOUT,
        )

        response = await self._client.get(
            _BRAVE_API_URL,
            params=params,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()

        results: list[SearchResult] = []
        web_data = data.get("web", {}) if isinstance(data, dict) else {}
        for item in web_data.get("results", []):
            title = str(item.get("title") or "")
            url = str(item.get("url") or "")
            snippet = str(item.get("description") or "")
            if url:
                results.append(SearchResult(title=title, url=url, snippet=snippet))

        return results

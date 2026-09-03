"""Serper (Google SERP) search provider."""

from __future__ import annotations

import logging
import httpx

from app.services.search.base import BaseSearchProvider
from app.services.search.models import SearchResult

log = logging.getLogger(__name__)

_SERPER_API_URL = "https://google.serper.dev/search"


class SerperSearchProvider(BaseSearchProvider):
    """Serper (Google Search API) provider."""

    @property
    def name(self) -> str:
        return "serper"

    @property
    def tier(self) -> int:
        return 1

    @property
    def is_available(self) -> bool:
        return bool(self._settings.SERPER_API_KEY)

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        if not self.is_available or not self._settings.SERPER_API_KEY:
            raise ValueError("Serper API key not configured")

        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self._settings.SERPER_API_KEY,
        }
        body = {
            "q": query,
            "num": max_results,
        }

        timeout = httpx.Timeout(
            timeout=float(self._settings.SEARCH_TIMEOUT),
            connect=self._settings.CONNECT_TIMEOUT,
        )

        response = await self._client.post(
            _SERPER_API_URL,
            json=body,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()

        results: list[SearchResult] = []
        for item in data.get("organic", []):
            title = str(item.get("title") or "")
            url = str(item.get("link") or "")
            snippet = str(item.get("snippet") or "")
            if url:
                results.append(SearchResult(title=title, url=url, snippet=snippet))

        return results

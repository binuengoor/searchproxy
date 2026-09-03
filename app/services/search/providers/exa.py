"""Exa AI search provider."""

from __future__ import annotations

import logging
import httpx

from app.services.search.base import BaseSearchProvider
from app.services.search.models import SearchResult

log = logging.getLogger(__name__)

_EXA_API_URL = "https://api.exa.ai/search"


class ExaSearchProvider(BaseSearchProvider):
    """Exa AI Search provider."""

    @property
    def name(self) -> str:
        return "exa"

    @property
    def tier(self) -> int:
        return 1

    @property
    def is_available(self) -> bool:
        return bool(self._settings.EXA_API_KEY)

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        if not self.is_available or not self._settings.EXA_API_KEY:
            raise ValueError("Exa API key not configured")

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._settings.EXA_API_KEY,
        }
        body = {
            "query": query,
            "numResults": max_results,
            "contents": {"text": {"maxCharacters": 500}},
        }

        timeout = httpx.Timeout(
            timeout=float(self._settings.SEARCH_TIMEOUT),
            connect=self._settings.CONNECT_TIMEOUT,
        )

        response = await self._client.post(
            _EXA_API_URL,
            json=body,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()

        results: list[SearchResult] = []
        for item in data.get("results", []):
            title = str(item.get("title") or "")
            url = str(item.get("url") or "")
            snippet = str(item.get("text") or "")
            if url:
                results.append(SearchResult(title=title, url=url, snippet=snippet))

        return results

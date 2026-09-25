"""SearXNG search provider (local fallback)."""

from __future__ import annotations

import logging

import httpx

from app.services.search.base import (
    BaseSearchProvider,
    normalize_domain,
    normalize_freshness,
)
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

    async def search(
        self,
        query: str,
        max_results: int = 10,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        freshness: str | None = None,
    ) -> list[SearchResult]:
        if not self.is_available or not self._settings.SEARXNG_URL:
            raise ValueError("SearXNG URL not configured")

        url = self._settings.SEARXNG_URL

        q = query
        if include_domains:
            inc = [normalize_domain(d) for d in include_domains if normalize_domain(d)]
            if len(inc) == 1:
                q = f"{q} site:{inc[0]}"
            elif len(inc) > 1:
                q = f"{q} ({' OR '.join(f'site:{d}' for d in inc)})"
        if exclude_domains:
            exc = [normalize_domain(d) for d in exclude_domains if normalize_domain(d)]
            for d in exc:
                q = f"{q} -site:{d}"

        params: dict[str, str | int] = {
            "q": q,
            "format": "json",
        }
        norm_freshness = normalize_freshness(freshness)
        if norm_freshness:
            params["time_range"] = norm_freshness

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

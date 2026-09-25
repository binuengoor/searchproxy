"""Serper (Google SERP) search provider."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.search.base import (
    BaseSearchProvider,
    normalize_domain,
    normalize_freshness,
)
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

    async def search(
        self,
        query: str,
        max_results: int = 10,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        freshness: str | None = None,
    ) -> list[SearchResult]:
        if not self.is_available or not self._settings.SERPER_API_KEY:
            raise ValueError("Serper API key not configured")

        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self._settings.SERPER_API_KEY,
        }

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

        body: dict[str, Any] = {
            "q": q,
            "num": max_results,
        }
        norm_freshness = normalize_freshness(freshness)
        if norm_freshness:
            body["tbs"] = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}[
                norm_freshness
            ]

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

"""Exa AI search provider."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.services.search.base import (
    BaseSearchProvider,
    normalize_domain,
    normalize_freshness,
)
from app.services.search.models import SearchResult

log = logging.getLogger(__name__)

_EXA_API_URL = "https://api.exa.ai/search"


def _freshness_to_exa_date(freshness: str | None) -> str | None:
    norm = normalize_freshness(freshness)
    if not norm:
        return None
    now = datetime.now(timezone.utc)
    days = {"day": 1, "week": 7, "month": 30, "year": 365}[norm]
    return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    async def search(
        self,
        query: str,
        max_results: int = 10,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        freshness: str | None = None,
    ) -> list[SearchResult]:
        if not self.is_available or not self._settings.EXA_API_KEY:
            raise ValueError("Exa API key not configured")

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._settings.EXA_API_KEY,
        }
        body: dict[str, Any] = {
            "query": query,
            "numResults": max_results,
            "contents": {"text": True},
        }
        if include_domains:
            inc = [normalize_domain(d) for d in include_domains if normalize_domain(d)]
            if inc:
                body["includeDomains"] = inc
        if exclude_domains:
            exc = [normalize_domain(d) for d in exclude_domains if normalize_domain(d)]
            if exc:
                body["excludeDomains"] = exc
        if freshness:
            start_date = _freshness_to_exa_date(freshness)
            if start_date:
                body["startPublishedDate"] = start_date

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
            text = str(item.get("text") or "")
            snippet = text[:500] if len(text) > 500 else text
            if url:
                results.append(
                    SearchResult(
                        title=title,
                        url=url,
                        snippet=snippet,
                        text=text if text else None,
                    )
                )

        return results

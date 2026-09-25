"""Base search provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.services.search.models import SearchResult


def normalize_domain(domain: str) -> str:
    """Extract clean domain/host (lowercase, no scheme, path, port, or trailing slash)."""
    d = domain.strip().lower()
    if not d:
        return ""
    if "://" in d:
        parsed = urlparse(d)
        return (parsed.hostname or "").lower()
    # Strip trailing path, query, fragment
    d = d.split("/")[0].split("?")[0].split("#")[0]
    return d.split(":")[0].strip()


def matches_domain(url: str, domains: set[str]) -> bool:
    """Return True if url's host matches or is a subdomain of any domain in domains."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    for d in domains:
        if not d:
            continue
        if host == d or host.endswith(f".{d}"):
            return True
    return False


def normalize_freshness(freshness: str | None) -> str | None:
    """Normalize freshness to one of: 'day', 'week', 'month', 'year', or None."""
    if not freshness:
        return None
    f = freshness.strip().lower()
    if f in ("day", "d", "pd", "qdr:d", "24h", "today", "past_day"):
        return "day"
    if f in ("week", "w", "pw", "qdr:w", "7d", "past_week"):
        return "week"
    if f in ("month", "m", "pm", "qdr:m", "30d", "past_month"):
        return "month"
    if f in ("year", "y", "py", "qdr:y", "365d", "past_year"):
        return "year"
    return None


class BaseSearchProvider(ABC):
    """Abstract base class for all search providers."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider identifier (e.g. 'tavily', 'brave', 'searxng')."""
        ...

    @property
    @abstractmethod
    def tier(self) -> int:
        """Tier level: 1 = Free API Quota, 2 = Fallback / Safety Net (SearXNG)."""
        ...

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """True if the provider has necessary API keys or configurations."""
        ...

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int = 10,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        freshness: str | None = None,
    ) -> list[SearchResult]:
        """Execute search and return normalized SearchResult list.

        Should raise exceptions on failure so SearchRouter can handle failover / cooldown.
        """
        ...

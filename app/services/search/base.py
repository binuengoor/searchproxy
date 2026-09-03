"""Base search provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
import httpx

from app.config import Settings
from app.services.search.models import SearchResult


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
    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Execute search and return normalized SearchResult list.

        Should raise exceptions on failure so SearchRouter can handle failover / cooldown.
        """
        ...

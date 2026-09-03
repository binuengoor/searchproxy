"""Search router orchestrating multi-provider quota rotation, circuit breaking, and SearXNG fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import httpx

from app.config import Settings
from app.services.search.base import BaseSearchProvider
from app.services.search.models import ProviderStatus, SearchResponse, SearchResult
from app.services.search.providers.brave import BraveSearchProvider
from app.services.search.providers.exa import ExaSearchProvider
from app.services.search.providers.searxng import SearxngSearchProvider
from app.services.search.providers.serper import SerperSearchProvider
from app.services.search.providers.tavily import TavilySearchProvider

if TYPE_CHECKING:
    from app.services.cache import CacheService

log = logging.getLogger(__name__)


class SearchRouter:
    """Orchestrates search execution across Tier 1 (free quotas) and Tier 2 (SearXNG)."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        cache: CacheService | None = None,
        custom_providers: list[BaseSearchProvider] | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._cache = cache
        self._lock = asyncio.Lock()
        self._tier1_index = 0
        self._cooldowns: dict[str, float] = {}
        self._stats: dict[str, ProviderStatus] = {}

        if custom_providers is not None:
            self._providers = custom_providers
        else:
            self._providers = [
                TavilySearchProvider(client=client, settings=settings),
                BraveSearchProvider(client=client, settings=settings),
                ExaSearchProvider(client=client, settings=settings),
                SerperSearchProvider(client=client, settings=settings),
                SearxngSearchProvider(client=client, settings=settings),
            ]

        for p in self._providers:
            self._stats[p.name] = ProviderStatus(
                name=p.name,
                tier=p.tier,
                is_available=p.is_available,
            )

    @property
    def tier1_providers(self) -> list[BaseSearchProvider]:
        """Available Tier 1 (Free API quota) providers."""
        return [p for p in self._providers if p.tier == 1 and p.is_available]

    @property
    def tier2_providers(self) -> list[BaseSearchProvider]:
        """Available Tier 2 (Safety Net / SearXNG) providers."""
        return [p for p in self._providers if p.tier == 2 and p.is_available]

    def _is_cooling_down(self, name: str) -> bool:
        cooldown_until = self._cooldowns.get(name, 0.0)
        return time.time() < cooldown_until

    def _set_cooldown(self, name: str, duration: float | None = None) -> None:
        secs = duration if duration is not None else float(self._settings.SEARCH_COOLDOWN_SECONDS)
        cooldown_until = time.time() + secs
        self._cooldowns[name] = cooldown_until
        if name in self._stats:
            self._stats[name].cooldown_until = cooldown_until
        log.warning("Search provider '%s' placed on cooldown for %d seconds (until %.0f)", name, secs, cooldown_until)

    async def search(self, query: str, max_results: int = 10) -> SearchResponse:
        """Search across providers with rotation, circuit breaking, and fallback.

        Graceful degradation: on complete failure across all providers,
        returns SearchResponse(results=[]) so callers never crash.
        """
        log.info("Searching across providers for '%s' (max_results=%d)", query, max_results)

        # 1. Read cache
        if self._cache is not None:
            cached = await self._cache.get_search(query, max_results)
            if cached is not None:
                log.info("Cache HIT for search: '%s'", query)
                try:
                    return SearchResponse.model_validate(cached)
                except Exception as exc:
                    log.warning("Cache deserialization failed for search '%s': %s", query, exc)
            else:
                log.info("Cache MISS for search: '%s'", query)

        results: list[SearchResult] = []
        tier1 = self.tier1_providers

        # 2. Try Tier 1 with round-robin rotation
        if tier1:
            async with self._lock:
                start_idx = self._tier1_index
                self._tier1_index = (self._tier1_index + 1) % len(tier1)

            # Order providers starting from current round-robin index
            ordered_tier1 = [tier1[(start_idx + i) % len(tier1)] for i in range(len(tier1))]

            for provider in ordered_tier1:
                if self._is_cooling_down(provider.name):
                    log.debug("Skipping '%s' (currently in cooldown)", provider.name)
                    continue

                stat = self._stats.get(provider.name)
                if stat:
                    stat.total_requests += 1

                try:
                    log.info("Attempting search via Tier 1 provider: '%s'", provider.name)
                    results = await provider.search(query=query, max_results=max_results)
                    if results:
                        log.info("Provider '%s' succeeded with %d results", provider.name, len(results))
                        break
                    else:
                        log.info("Provider '%s' returned 0 results; trying next provider", provider.name)
                except httpx.HTTPStatusError as exc:
                    if stat:
                        stat.failed_requests += 1
                        stat.last_error = f"HTTP {exc.response.status_code}"
                    if exc.response.status_code == 429:
                        log.warning("Provider '%s' returned 429 Too Many Requests (quota exceeded)", provider.name)
                        self._set_cooldown(provider.name)
                    else:
                        log.warning("Provider '%s' failed with HTTP %d", provider.name, exc.response.status_code)
                        self._set_cooldown(provider.name, duration=60.0)
                except Exception as exc:
                    if stat:
                        stat.failed_requests += 1
                        stat.last_error = str(exc)
                    log.warning("Provider '%s' error: %s", provider.name, exc)
                    self._set_cooldown(provider.name, duration=30.0)

        # 3. Fallback to Tier 2 (SearXNG safety net) if Tier 1 failed or returned nothing
        if not results:
            tier2 = self.tier2_providers
            for provider in tier2:
                if self._is_cooling_down(provider.name):
                    log.debug("Skipping fallback '%s' (in cooldown)", provider.name)
                    continue

                stat = self._stats.get(provider.name)
                if stat:
                    stat.total_requests += 1

                try:
                    log.info("Attempting search via fallback safety net: '%s'", provider.name)
                    results = await provider.search(query=query, max_results=max_results)
                    if results:
                        log.info("Fallback '%s' succeeded with %d results", provider.name, len(results))
                        break
                except Exception as exc:
                    if stat:
                        stat.failed_requests += 1
                        stat.last_error = str(exc)
                    log.warning("Fallback provider '%s' failed: %s", provider.name, exc)
                    self._set_cooldown(provider.name, duration=60.0)

        response = SearchResponse(results=results)

        # 4. Write cache on success
        if results and self._cache is not None:
            try:
                await self._cache.set_search(query, max_results, response.model_dump())
            except Exception as exc:
                log.warning("Failed to cache search results for '%s': %s", query, exc)

        return response

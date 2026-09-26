"""Search router orchestrating quota rotation, circuit breaking, and SearXNG fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.services.search.base import BaseSearchProvider, normalize_domain, normalize_freshness
from app.services.search.models import ProviderStatus, SearchResponse, SearchResult
from app.services.search.providers.brave import BraveSearchProvider
from app.services.search.providers.exa import ExaSearchProvider
from app.services.search.providers.searxng import SearxngSearchProvider
from app.services.search.providers.serper import SerperSearchProvider
from app.services.search.providers.tavily import TavilySearchProvider

if TYPE_CHECKING:
    from app.services.cache import CacheService

log = logging.getLogger(__name__)


def _canonical_key(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{path}"


def reciprocal_rank_fusion(
    result_lists: list[list[SearchResult]],
    k: int = 60,
    max_results: int = 10,
) -> list[SearchResult]:
    """Combine multiple ranked search results using Reciprocal Rank Fusion (RRF)."""
    scores: dict[str, float] = {}
    doc_map: dict[str, SearchResult] = {}

    for r_list in result_lists:
        for rank, item in enumerate(r_list, start=1):
            key = _canonical_key(item.url)
            scores[key] = scores.get(key, 0.0) + (1.0 / (k + rank))
            if key not in doc_map:
                doc_map[key] = item
            else:
                existing = doc_map[key]
                existing_text = getattr(existing, "text", None)
                item_text = getattr(item, "text", None)
                best_text = item_text or existing_text

                # Prefer item if it has text or longer snippet
                if item_text and not existing_text:
                    chosen = item
                elif existing_text and not item_text:
                    chosen = existing
                elif len(item.snippet) > len(existing.snippet):
                    chosen = item
                else:
                    chosen = existing

                # Ensure chosen keeps best_text so instant bypass is never wiped
                if not getattr(chosen, "text", None) and best_text:
                    chosen = SearchResult(
                        title=chosen.title,
                        url=chosen.url,
                        snippet=chosen.snippet,
                        text=best_text,
                    )
                doc_map[key] = chosen

    sorted_keys = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    return [doc_map[k] for k in sorted_keys[:max_results]]


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
        log.warning(
            "Search provider '%s' placed on cooldown for %d seconds (until %.0f)",
            name,
            secs,
            cooldown_until,
        )

    def _is_degraded(self, name: str) -> bool:
        stat = self._stats.get(name)
        if not stat:
            return False
        return stat.is_degraded

    def _record_success(self, name: str, latency_seconds: float) -> None:
        stat = self._stats.get(name)
        if not stat:
            return
        stat.rolling_latencies.append(latency_seconds)
        if len(stat.rolling_latencies) > 10:
            stat.rolling_latencies.pop(0)

        stat.avg_latency_ms = (
            sum(stat.rolling_latencies) / len(stat.rolling_latencies) * 1000.0
        )
        if stat.recent_errors > 0:
            stat.recent_errors = max(0, stat.recent_errors - 1)

        # Degraded if avg latency > 4000ms (at least 3 requests) or recent errors >= 3
        stat.is_degraded = (
            (len(stat.rolling_latencies) >= 3 and stat.avg_latency_ms > 4000.0)
            or stat.recent_errors >= 3
        )

    def _record_failure(self, name: str, exc: Exception) -> None:
        stat = self._stats.get(name)
        if stat:
            stat.failed_requests += 1
            stat.recent_errors += 1
            stat.last_error = str(exc)
            if stat.recent_errors >= 3:
                stat.is_degraded = True
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
            self._set_cooldown(name)
        else:
            self._set_cooldown(name, duration=30.0)

    async def _hybrid_search(
        self,
        query: str,
        max_results: int,
        clean_inc: list[str] | None,
        clean_exc: list[str] | None,
        clean_freshness: str | None,
        call_fn: Any,
    ) -> list[SearchResult]:
        """Execute hybrid search combining a lexical engine and a semantic engine (Exa) with RRF."""
        # Define timed caller to track rolling latency and error counts for hybrid providers
        async def _timed_call(p: BaseSearchProvider) -> list[SearchResult]:
            stat = self._stats.get(p.name)
            if stat:
                stat.total_requests += 1
            t0 = time.perf_counter()
            try:
                res = await call_fn(p)
                elapsed = time.perf_counter() - t0
                self._record_success(p.name, elapsed)
                return res
            except Exception as exc:
                self._record_failure(p.name, exc)
                raise

        sem_provider = next(
            (
                p
                for p in self._providers
                if p.name == "exa" and p.is_available and not self._is_cooling_down(p.name)
            ),
            None,
        )

        # Select lexical provider: round-robin rotate among healthy Tier 1 providers
        tier1_lex_candidates = [
            p
            for p in self.tier1_providers
            if p.name in ("brave", "tavily", "serper")
            and not self._is_cooling_down(p.name)
        ]

        if tier1_lex_candidates:
            async with self._lock:
                start_idx = self._tier1_index
                self._tier1_index = (self._tier1_index + 1) % len(self.tier1_providers)
            ordered_lex = [
                tier1_lex_candidates[(start_idx + i) % len(tier1_lex_candidates)]
                for i in range(len(tier1_lex_candidates))
            ]
            # Try healthy first, then degraded
            lex_provider = next((p for p in ordered_lex if not self._is_degraded(p.name)), None)
            if lex_provider is None:
                lex_provider = ordered_lex[0]
        else:
            lex_provider = None

        if lex_provider is None:
            # Fall back to any other non-cooling-down Tier 1 provider (except semantic engine)
            lex_provider = next(
                (
                    p
                    for p in self.tier1_providers
                    if (sem_provider is None or p.name != sem_provider.name)
                    and not self._is_cooling_down(p.name)
                ),
                None,
            )

        if lex_provider is None:
            # SearXNG is strictly final fallback (Tier 2)
            lex_provider = next(
                (
                    p
                    for p in self.tier2_providers
                    if not self._is_cooling_down(p.name)
                ),
                None,
            )

        # Fallback for custom / mock providers: strictly prioritize Tier 1 before Tier 2
        if lex_provider is None or sem_provider is None:
            tier1_avail = [
                p
                for p in self.tier1_providers
                if not self._is_cooling_down(p.name)
            ]
            tier2_avail = [
                p
                for p in self.tier2_providers
                if not self._is_cooling_down(p.name)
            ]
            avail = tier1_avail + tier2_avail
            if sem_provider is None:
                sem_candidates = [
                    p
                    for p in avail
                    if lex_provider is None or p.name != lex_provider.name
                ]
                if sem_candidates:
                    sem_provider = sem_candidates[0]
            if lex_provider is None:
                lex_candidates = [
                    p
                    for p in avail
                    if sem_provider is None or p.name != sem_provider.name
                ]
                if lex_candidates:
                    lex_provider = lex_candidates[0]

        if not sem_provider or not lex_provider or sem_provider.name == lex_provider.name:
            provider = sem_provider or lex_provider
            if provider:
                try:
                    return await _timed_call(provider)
                except Exception as exc:
                    log.warning(
                        "Single hybrid fallback provider '%s' failed: %s",
                        provider.name,
                        exc,
                    )
            return []

        log.info(
            "Executing RRF hybrid search: lexical='%s', semantic='%s'",
            lex_provider.name,
            sem_provider.name,
        )
        tasks = [_timed_call(lex_provider), _timed_call(sem_provider)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        lex_res: list[SearchResult] = results[0] if isinstance(results[0], list) else []
        sem_res: list[SearchResult] = results[1] if isinstance(results[1], list) else []

        if isinstance(results[0], Exception):
            log.warning(
                "Lexical provider '%s' failed during hybrid search: %s",
                lex_provider.name,
                results[0],
            )
        if isinstance(results[1], Exception):
            log.warning(
                "Semantic provider '%s' failed during hybrid search: %s",
                sem_provider.name,
                results[1],
            )

        if lex_res and sem_res:
            fused = reciprocal_rank_fusion([lex_res, sem_res], k=60, max_results=max_results)
            log.info(
                "RRF fusion combined %d lexical and %d semantic results -> %d fused results",
                len(lex_res),
                len(sem_res),
                len(fused),
            )
            return fused
        elif lex_res:
            return lex_res[:max_results]
        elif sem_res:
            return sem_res[:max_results]
        return []

    async def search(
        self,
        query: str,
        max_results: int = 10,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        freshness: str | None = None,
        hybrid: bool = False,
    ) -> SearchResponse:
        """Search across providers with rotation, circuit breaking, fallback, or RRF hybrid."""
        log.info(
            "Searching across providers for '%s' (max_results=%d, "
            "include_domains=%s, exclude_domains=%s, freshness=%s, hybrid=%s)",
            query,
            max_results,
            include_domains,
            exclude_domains,
            freshness,
            hybrid,
        )

        clean_inc = (
            list(dict.fromkeys(normalize_domain(d) for d in include_domains if normalize_domain(d)))
            if include_domains
            else None
        )
        clean_exc = (
            list(dict.fromkeys(normalize_domain(d) for d in exclude_domains if normalize_domain(d)))
            if exclude_domains
            else None
        )
        clean_freshness = normalize_freshness(freshness)

        # 1. Read cache
        if self._cache is not None:
            cached = await self._cache.get_search(
                query,
                max_results,
                include_domains=clean_inc,
                exclude_domains=clean_exc,
                freshness=clean_freshness,
            )
            if cached is not None:
                log.info("Cache HIT for search: '%s'", query)
                try:
                    return SearchResponse.model_validate(cached)
                except Exception as exc:
                    log.warning("Cache deserialization failed for search '%s': %s", query, exc)
            else:
                log.info("Cache MISS for search: '%s'", query)

        results: list[SearchResult] = []

        async def _call_provider(p: BaseSearchProvider) -> list[SearchResult]:
            try:
                return await p.search(
                    query=query,
                    max_results=max_results,
                    include_domains=clean_inc,
                    exclude_domains=clean_exc,
                    freshness=clean_freshness,
                )
            except TypeError as exc:
                msg = str(exc)
                if "unexpected keyword argument" in msg or "too many positional arguments" in msg:
                    return await p.search(query=query, max_results=max_results)
                raise

        # 2. Hybrid Search (RRF) if requested
        if hybrid:
            results = await self._hybrid_search(
                query=query,
                max_results=max_results,
                clean_inc=clean_inc,
                clean_exc=clean_exc,
                clean_freshness=clean_freshness,
                call_fn=_call_provider,
            )

        # 3. Standard Tier 1 with round-robin rotation (if not hybrid or hybrid returned nothing)
        tier1 = self.tier1_providers
        if not results and tier1:
            async with self._lock:
                start_idx = self._tier1_index
                self._tier1_index = (self._tier1_index + 1) % len(tier1)

            ordered_tier1 = [tier1[(start_idx + i) % len(tier1)] for i in range(len(tier1))]

            # Latency and quality-aware ordering: prioritize healthy over degraded providers
            # while preserving round-robin quota fairness
            healthy_tier1 = [
                p
                for p in ordered_tier1
                if not self._is_cooling_down(p.name) and not self._is_degraded(p.name)
            ]
            degraded_tier1 = [
                p
                for p in ordered_tier1
                if not self._is_cooling_down(p.name) and self._is_degraded(p.name)
            ]
            tier1_to_try = healthy_tier1 + degraded_tier1

            for provider in tier1_to_try:
                stat = self._stats.get(provider.name)
                if stat:
                    stat.total_requests += 1

                try:
                    log.info("Attempting search via Tier 1 provider: '%s'", provider.name)
                    t0 = time.perf_counter()
                    results = await _call_provider(provider)
                    elapsed = time.perf_counter() - t0
                    self._record_success(provider.name, elapsed)

                    if results:
                        log.info(
                            "Provider '%s' succeeded with %d results (%.2fs)",
                            provider.name,
                            len(results),
                            elapsed,
                        )
                        break
                    else:
                        log.info("Provider '%s' returned 0 results; trying next", provider.name)
                except httpx.HTTPStatusError as exc:
                    self._record_failure(provider.name, exc)
                    if exc.response.status_code == 429:
                        log.warning(
                            "Provider '%s' returned 429 Too Many Requests",
                            provider.name,
                        )
                    else:
                        log.warning(
                            "Provider '%s' failed with HTTP %d",
                            provider.name,
                            exc.response.status_code,
                        )
                except Exception as exc:
                    self._record_failure(provider.name, exc)
                    log.warning("Provider '%s' error: %s", provider.name, exc)

        # 4. Fallback to Tier 2 (SearXNG strictly as final fallback)
        # if Tier 1 failed or returned nothing
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
                    log.info(
                        "Attempting search via final fallback safety net (Tier 2): '%s'",
                        provider.name,
                    )
                    t0 = time.perf_counter()
                    results = await _call_provider(provider)
                    elapsed = time.perf_counter() - t0
                    self._record_success(provider.name, elapsed)
                    if results:
                        log.info(
                            "Fallback '%s' succeeded with %d results (%.2fs)",
                            provider.name,
                            len(results),
                            elapsed,
                        )
                        break
                except Exception as exc:
                    self._record_failure(provider.name, exc)
                    log.warning("Fallback provider '%s' failed: %s", provider.name, exc)

        response = SearchResponse(results=results)

        # 5. Write cache on success
        if results and self._cache is not None:
            try:
                await self._cache.set_search(
                    query,
                    max_results,
                    response.model_dump(),
                    include_domains=clean_inc,
                    exclude_domains=clean_exc,
                    freshness=clean_freshness,
                )
            except Exception as exc:
                log.warning("Failed to cache search results for '%s': %s", query, exc)

        return response

"""Fetch chain orchestrator — tiered fetch with anti-bot firebreak."""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx
from pydantic import BaseModel, Field

from app.clean_executor import get_executor
from app.config import Settings
from app.services.content_cleaner import clean_content
from app.services.models import FetchResult
from app.services.crawl4ai import Crawl4AIClient
from app.middleware.correlation import _current_correlation_id
from app.services.metrics import get_collector
from app.services.jina_reader import JinaReaderClient
from app.services.scraperapi import ScraperAPIClient
from app.services.scrape_do import ScrapeDoClient

log = logging.getLogger(__name__)

# Case-insensitive anti-bot indicators in response body
_ANTI_BOT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"cloudflare", re.IGNORECASE),
    re.compile(r"just a moment", re.IGNORECASE),
    re.compile(r"checking your browser", re.IGNORECASE),
    re.compile(r"ddos-guard", re.IGNORECASE),
]


def _is_anti_bot_block(status_code: int | None, body: str) -> bool:
    """Return True if the response looks like an anti-bot challenge page."""
    if status_code == 403:
        return True
    if body:
        for pattern in _ANTI_BOT_PATTERNS:
            if pattern.search(body):
                return True
    return False


class FetchChain:
    """Orchestrates the tiered fetch chain: Crawl4AI → Jina Reader → anti-bot firebreak.

    Anti-bot services (Scrape.do, ScraperAPI) are only invoked when a response
    is a confirmed anti-bot block (403 or Cloudflare indicators in body).
    They are NEVER invoked for routine 5xx or timeout failures.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        cache: "CacheService | None" = None,
    ) -> None:
        self._settings = settings
        self._cache = cache
        self._crawl4ai = Crawl4AIClient(client=client, settings=settings)
        self._jina = JinaReaderClient(client=client, settings=settings)
        self._scrape_do = ScrapeDoClient(client=client, settings=settings)
        self._scraper_api = ScraperAPIClient(client=client, settings=settings)

    def _is_anti_bot(self, result: FetchResult) -> bool:
        """Check if a FetchResult indicates an anti-bot block."""
        if result.status_code == 403:
            return True
        if _is_anti_bot_block(result.status_code, result.markdown):
            return True
        return False

    def _is_transient(self, result: FetchResult) -> bool:
        """Return True if the result looks like a transient failure worth retrying."""
        if result.error == "timeout":
            return True
        if result.status_code is not None and result.status_code >= 500:
            return True
        return False

    async def execute(
        self,
        url: str,
        aggressive_clean: bool = False,
        skip_firebreak: bool = False,
        content_filter: str | None = None,
        content_query: str | None = None,
    ) -> FetchResult:
        """Execute the tiered fetch chain for the given URL.

        Flow:
        1. Crawl4AI.fetch_markdown — primary (with 1 retry on transient failure)
           ├── Success → return (cleaned)
           └── Failure (transient)
               ├── Retry once after 1s delay
               ├── Success → return (cleaned)
               └── Still failure
                   ├── Is anti-bot block? → skip Jina, go to firebreak
                   └── Other error → Jina Reader
                       ├── Success → return (cleaned)
                       └── Failure / is anti-bot? → firebreak

        Anti-Bot Firebreak:
        1. Scrape.do (if key set)
           ├── Success → return (cleaned)
           └── Failure → ScraperAPI (if key set)
               ├── Success → return (cleaned)
               └── Failure → all tiers exhausted

        Content cleaning: every successful result is passed through
        :func:`clean_content` so raw HTML from firebreak tiers is
        reduced to agent-friendly markdown.

        Args:
            url: The target URL to fetch.
            aggressive_clean: If True, run aggressive boilerplate removal.
            skip_firebreak: If True, skip anti-bot paid services (Scrape.do,
                ScraperAPI). Used during speculative prefetch to avoid wasting
                paid API calls on URLs that rerank may discard.
            content_filter: Crawl4AI filter mode ('fit', 'bm25', 'raw').
                When 'bm25', content_query must also be provided.
            content_query: BM25 query for Crawl4AI content filtering.

        Returns:
            FetchResult from the first successful tier, or a failure result
            if all tiers are exhausted. Includes fetch_time_ms for observability.
        """
        start_time = time.perf_counter()

        # ── Cache read ────────────────────────────────────────────────
        if self._cache is not None:
            cached = await self._cache.get_fetch(url)
            if cached is not None:
                log.info("Cache HIT for fetch: %s", url)
                try:
                    return FetchResult.model_validate(cached)
                except Exception as exc:
                    log.warning("Cache deserialization failed for %s: %s", url, exc)
            else:
                log.info("Cache MISS for fetch: %s", url)

        # ── Tier 1: Crawl4AI (with 1 transient retry) ────────────────────
        result = await self._crawl4ai.fetch_markdown(
            url, content_filter=content_filter, content_query=content_query,
        )

        if not result.success and self._is_transient(result):
            log.warning(
                "Crawl4AI transient failure for %s (status=%s, error=%s); retrying once after 1s",
                url,
                result.status_code,
                result.error,
            )
            await asyncio.sleep(1.0)
            result = await self._crawl4ai.fetch_markdown(
                url, content_filter=content_filter, content_query=content_query,
            )

        if result.success:
            if _is_anti_bot_block(result.status_code, result.markdown):
                log.warning(
                    "Crawl4AI returned %s but anti-bot content detected for %s, escalating to firebreak",
                    result.status_code,
                    url,
                )
                result.fetch_time_ms = self._elapsed_ms(start_time)
                return await self._firebreak_and_cache(url, start_time, aggressive_clean=aggressive_clean)
            log.info("Crawl4AI succeeded for %s", url)
            get_collector().inc_tier("crawl4ai", "success")
            loop = asyncio.get_running_loop()
            result.markdown = await loop.run_in_executor(
                get_executor(), clean_content, result.markdown, url, aggressive_clean,
            )
            result.fetch_time_ms = self._elapsed_ms(start_time)
            await self._store_fetch(url, result)
            return result

        # Crawl4AI failed permanently — determine if it's an anti-bot block
        if self._is_anti_bot(result):
            if skip_firebreak:
                log.info("Crawl4AI anti-bot for %s but skip_firebreak=True; returning failure", url)
                result.fetch_time_ms = self._elapsed_ms(start_time)
                return result
            log.warning(
                "Crawl4AI failed with %s — anti-bot detected, skipping Jina, escalating to firebreak",
                result.status_code,
            )
            result.fetch_time_ms = self._elapsed_ms(start_time)
            return await self._firebreak_and_cache(url, start_time, aggressive_clean=aggressive_clean)

        # Non-anti-bot failure — try Jina Reader
        log.info("Crawl4AI failed for %s (not anti-bot), trying Jina", url)
        jina_result = await self._jina.fetch(url)
        if jina_result.success:
            # Jina returned HTTP 200, but check if the body is actually an anti-bot page
            if _is_anti_bot_block(jina_result.status_code, jina_result.markdown):
                if skip_firebreak:
                    log.info("Jina anti-bot for %s but skip_firebreak=True; returning failure", url)
                    jina_result.fetch_time_ms = self._elapsed_ms(start_time)
                    return jina_result
                log.warning(
                    "Jina Reader returned %s but anti-bot content detected for %s, escalating to firebreak",
                    jina_result.status_code,
                    url,
                )
                jina_result.fetch_time_ms = self._elapsed_ms(start_time)
                return await self._firebreak_and_cache(url, start_time, aggressive_clean=aggressive_clean)
            log.info("Jina Reader succeeded for %s", url)
            get_collector().inc_tier("jina", "success")
            loop = asyncio.get_running_loop()
            jina_result.markdown = await loop.run_in_executor(
                get_executor(), clean_content, jina_result.markdown, url, aggressive_clean,
            )
            jina_result.fetch_time_ms = self._elapsed_ms(start_time)
            await self._store_fetch(url, jina_result)
            return jina_result

        # Jina failed — only firebreak for confirmed anti-bot blocks
        if self._is_anti_bot(jina_result):
            if skip_firebreak:
                log.info("Jina anti-bot for %s but skip_firebreak=True; returning failure", url)
                jina_result.fetch_time_ms = self._elapsed_ms(start_time)
                return jina_result
            log.warning(
                "Jina Reader failed with %s — anti-bot detected, escalating to firebreak",
                jina_result.status_code,
            )
            jina_result.fetch_time_ms = self._elapsed_ms(start_time)
            return await self._firebreak_and_cache(url, start_time, aggressive_clean=aggressive_clean)

        # Not anti-bot — all public tiers exhausted, return failure directly
        log.info(
            "Jina Reader failed for %s (not anti-bot) — all tiers exhausted",
            url,
        )
        get_collector().inc_tier("jina", "fail")
        jina_result.fetch_time_ms = self._elapsed_ms(start_time)
        await self._store_fetch(url, jina_result)
        return jina_result

    async def _store_fetch(self, url: str, result: FetchResult) -> None:
        """Store a fetch result in the cache if caching is enabled."""
        if self._cache is not None:
            await self._cache.set_fetch(url, result.model_dump())

    async def _firebreak_and_cache(self, url: str, start_time: float, aggressive_clean: bool = False) -> FetchResult:
        """Run firebreak then store the result in cache."""
        result = await self._firebreak(url, start_time, aggressive_clean=aggressive_clean)
        await self._store_fetch(url, result)
        return result

    async def _firebreak(self, url: str, start_time: float, aggressive_clean: bool = False) -> FetchResult:
        """Execute the anti-bot firebreak: Scrape.do and ScraperAPI in parallel.

        Only called for confirmed anti-bot blocks. Never called for routine
        5xx or timeout failures. Inherits start_time for end-to-end timing.

        Both anti-bot services run concurrently — the first successful result
        wins, and the other is cancelled. This reduces latency by the time
        the first service would have spent waiting for the second sequentially.
        """
        tasks: list[asyncio.Task] = []
        task_names: dict[asyncio.Task, str] = {}

        if self._settings.SCRAPE_DO_API_KEY:
            t = asyncio.create_task(self._scrape_do.fetch(url), name="firebreak:scrape_do")
            tasks.append(t)
            task_names[t] = "scrape_do"
        else:
            log.info("Scrape.do skipped: SCRAPE_DO_API_KEY not set")

        if self._settings.SCRAPERAPI_API_KEY:
            t = asyncio.create_task(self._scraper_api.fetch(url), name="firebreak:scraperapi")
            tasks.append(t)
            task_names[t] = "scraperapi"
        else:
            log.info("ScraperAPI skipped: SCRAPERAPI_API_KEY not set")

        if not tasks:
            log.warning("No anti-bot services configured for %s", url)
            return FetchResult(
                success=False,
                url=url,
                error="all tiers exhausted",
                source="",
                fetch_time_ms=self._elapsed_ms(start_time),
            )

        # Return the first successful result; cancel remaining tasks
        result: FetchResult | None = None
        try:
            for coro in asyncio.as_completed(tasks):
                task_result = await coro
                if task_result.success:
                    loop = asyncio.get_running_loop()
                    task_result.markdown = await loop.run_in_executor(
                        get_executor(), clean_content, task_result.markdown, url, aggressive_clean,
                    )
                    source_name = task_result.source or "anti_bot"
                    log.info("Anti-bot firebreak succeeded for %s via %s", url, source_name)
                    get_collector().inc_tier(source_name, "success")
                    task_result.fetch_time_ms = self._elapsed_ms(start_time)
                    result = task_result
                    break
                else:
                    source_name = task_result.source or "anti_bot"
                    get_collector().inc_tier(source_name, "fail")
                    log.warning("Anti-bot tier %s failed for %s: %s", source_name, url, task_result.error)
        except Exception as exc:
            log.warning("Anti-bot firebreak error for %s: %s", url, exc)
        finally:
            # Cancel any still-running tasks
            for t in tasks:
                if not t.done():
                    t.cancel()

        if result is not None:
            return result

        log.warning("All anti-bot tiers exhausted for %s", url)
        return FetchResult(
            success=False,
            url=url,
            error="all tiers exhausted",
            source="",
            fetch_time_ms=self._elapsed_ms(start_time),
        )

    @staticmethod
    def _elapsed_ms(start_time: float) -> float:
        """Return milliseconds elapsed since start_time."""
        return round((time.perf_counter() - start_time) * 1000, 2)
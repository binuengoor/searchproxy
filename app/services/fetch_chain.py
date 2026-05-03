"""Fetch chain orchestrator — tiered fetch with anti-bot firebreak."""

from __future__ import annotations

import logging
import re
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.services.content_cleaner import clean_content
from app.services.crawl4ai import Crawl4AIClient, FetchResult
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

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._settings = settings
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

    async def execute(self, url: str) -> FetchResult:
        """Execute the tiered fetch chain for the given URL.

        Flow:
        1. Crawl4AI.fetch_markdown — primary
           ├── Success → return (cleaned)
           └── Failure
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

        Returns:
            FetchResult from the first successful tier, or a failure result
            if all tiers are exhausted.
        """
        # ── Tier 1: Crawl4AI ────────────────────────────────────────────────
        result = await self._crawl4ai.fetch_markdown(url)
        if result.success:
            # Even on success, check if the body contains anti-bot content
            if _is_anti_bot_block(result.status_code, result.markdown):
                log.warning(
                    "Crawl4AI returned %s but anti-bot content detected for %s, escalating to firebreak",
                    result.status_code,
                    url,
                )
                return await self._firebreak(url)
            log.info("Crawl4AI succeeded for %s", url)
            result.markdown = clean_content(result.markdown, url=url)
            return result

        # Crawl4AI failed — determine if it's an anti-bot block
        is_antibot = self._is_anti_bot(result)
        if is_antibot:
            log.warning(
                "Crawl4AI failed with %s — anti-bot detected, skipping Jina, escalating to firebreak",
                result.status_code,
            )
            return await self._firebreak(url)

        # Non-anti-bot failure — try Jina Reader
        log.info("Crawl4AI failed for %s (not anti-bot), trying Jina", url)
        jina_result = await self._jina.fetch(url)
        if jina_result.success:
            # Jina returned HTTP 200, but check if the body is actually an anti-bot page
            if _is_anti_bot_block(jina_result.status_code, jina_result.markdown):
                log.warning(
                    "Jina Reader returned %s but anti-bot content detected for %s, escalating to firebreak",
                    jina_result.status_code,
                    url,
                )
                return await self._firebreak(url)
            log.info("Jina Reader succeeded for %s", url)
            jina_result.markdown = clean_content(jina_result.markdown, url=url)
            return jina_result

        # Jina failed — only firebreak for confirmed anti-bot blocks
        if self._is_anti_bot(jina_result):
            log.warning(
                "Jina Reader failed with %s — anti-bot detected, escalating to firebreak",
                jina_result.status_code,
            )
            return await self._firebreak(url)

        # Not anti-bot — all public tiers exhausted, return failure directly
        log.info(
            "Jina Reader failed for %s (not anti-bot) — all tiers exhausted",
            url,
        )
        return jina_result

    async def _firebreak(self, url: str) -> FetchResult:
        """Execute the anti-bot firebreak: Scrape.do → ScraperAPI.

        Only called for confirmed anti-bot blocks. Never called for routine
        5xx or timeout failures.
        """
        # ── Tier 2a: Scrape.do ─────────────────────────────────────────────
        if self._settings.SCRAPE_DO_API_KEY:
            scrape_do_result = await self._scrape_do.fetch(url)
            if scrape_do_result.success:
                scrape_do_result.markdown = clean_content(scrape_do_result.markdown, url=url)
                log.info("Scrape.do succeeded for %s after cleaning", url)
                return scrape_do_result
            log.warning("Scrape.do failed for %s, trying ScraperAPI", url)
        else:
            log.info("Scrape.do skipped: SCRAPE_DO_API_KEY not set")

        # ── Tier 2b: ScraperAPI ────────────────────────────────────────────
        if self._settings.SCRAPERAPI_API_KEY:
            scraper_api_result = await self._scraper_api.fetch(url)
            if scraper_api_result.success:
                scraper_api_result.markdown = clean_content(scraper_api_result.markdown, url=url)
                log.info("ScraperAPI succeeded for %s after cleaning", url)
                return scraper_api_result
            log.warning("ScraperAPI failed for %s", url)
        else:
            log.info("ScraperAPI skipped: SCRAPERAPI_API_KEY not set")

        # All tiers exhausted
        log.error("All fetch tiers exhausted for %s", url)
        return FetchResult(
            success=False,
            url=url,
            error="all tiers exhausted",
            source="",
        )

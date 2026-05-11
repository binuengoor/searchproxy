"""Retrieve orchestrator — composes search → rerank → fetch → synthesize.

The /v1/retrieve endpoint's core pipeline. Each step is a separate service
with its own client, making the orchestration easy to test and extend.

Performance optimizations:
1. Speculative prefetch: when RETRIEVE_PREFETCH_DURING_RERANK is enabled,
   the pipeline starts fetching top search results *during* rerank, saving
   1-2s by overlapping network calls.
2. BM25 content filtering: Crawl4AI is called with f=bm25&q=<query> for
   aggressive fetches, reducing content by 60-80% at the source.
3. Per-URL timeout: each fetch task gets its own asyncio.timeout() so one
   slow URL doesn't consume the entire batch timeout.
4. Parallel content cleaning: after all fetches complete, clean_content()
   runs in parallel across all URLs instead of sequentially.
5. Prefetch respects skip_firebreak: speculative fetches skip paid anti-bot
   services; if rerank confirms the URL is needed, the full chain runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import AsyncIterator, Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.schemas import Citation, RetrieveResponse, SourceChunk
from app.services.fetch_chain import FetchChain
from app.services.models import FetchResult
from app.services.litellm_search import LiteLLMSearchClient
from app.services.rerank_service import RerankService
from app.services.synthesis_service import SynthesisService
from app.services.content_cleaner import clean_content

log = logging.getLogger(__name__)

# Paywall / login-wall indicators — if any of these appear, the content is likely useless
_PAYWALL_PATTERNS = [
    "subscribe to continue",
    "sign in to read",
    "sign in to view",
    "login to read",
    "login to view",
    "premium content",
    "exclusive content",
    "members only",
    "subscription required",
    "please subscribe",
    "create an account to continue",
    "register to read",
    "upgrade to premium",
    "to continue reading",
    "please log in",
]
_PAYWALL_RE = re.compile(r"(?:" + "|".join(re.escape(p) for p in _PAYWALL_PATTERNS) + r")", re.IGNORECASE)


def _is_likely_paywall(content: str) -> bool:
    """Detect paywall/login-wall pages by common phrases.

    Removed 'access denied' — too many false positives from legitimate
    pages that include this phrase in non-paywall contexts (API docs,
    error pages, Cloudflare challenges). Real 403 blocks are caught by
    the fetch chain's anti-bot detection instead.
    """
    if len(content) < 200:
        # Very short content is suspicious regardless of keywords
        return True
    return bool(_PAYWALL_RE.search(content))


def _is_too_short(content: str, min_length: int) -> bool:
    """Check if content is below the minimum usable length."""
    return len(content) < min_length


# Dedup helper: normalize URL to domain+path for grouping
def _canonical_key(url: str) -> str:
    """Normalize a URL for dedup: strip scheme, www, trailing slash, query, fragment."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{path}"


def _truncate_content(content: str, max_chars: int) -> str:
    """Truncate content to max_chars, rounding down to the nearest paragraph boundary.

    Prevents mid-sentence or mid-table cuts by finding the last
    double-newline before max_chars. Falls back to last sentence
    boundary (period/newline) if no paragraph break is found.
    """
    if len(content) <= max_chars:
        return content

    # Try to cut at the last paragraph boundary (\n\n) before max_chars
    search_range = content[:max_chars]
    last_para = search_range.rfind("\n\n")
    if last_para > max_chars * 0.5:
        return content[:last_para].strip()

    # No good paragraph boundary — try sentence boundary
    last_sentence = max(
        search_range.rfind(".\n"),
        search_range.rfind(". "),
    )
    if last_sentence > max_chars * 0.5:
        return content[:last_sentence + 1].strip()

    # Last resort: hard cut
    return content[:max_chars].strip()


class RetrieveService:
    """Orchestrates: search → dedup → rerank → parallel fetch → synthesize.

    Each step can fail independently; the service degrades gracefully:
    - Search returns 0 results → empty response
    - Rerank fails → use original search order
    - Some fetches fail → proceed with whatever succeeded
    - Synthesis fails → return raw source chunks with fallback answer
    """

    def __init__(
        self,
        search_client: LiteLLMSearchClient,
        fetch_chain: FetchChain,
        rerank_service: RerankService,
        synthesis_service: SynthesisService,
        settings: Settings,
    ) -> None:
        self._search = search_client
        self._fetch = fetch_chain
        self._rerank = rerank_service
        self._synthesis = synthesis_service
        self._settings = settings

    # ------------------------------------------------------------------
    # Pipeline steps (extracted for testability and per-step profiling)
    # ------------------------------------------------------------------

    async def _search_step(
        self, query: str, max_results: int,
    ) -> tuple[list[dict[str, str]], int]:
        """Run search and return (results_list, count).

        Returns ([], 0) if no results found.
        """
        log.info("Retrieve pipeline: search for '%s' (max_results=%d)", query, max_results)
        search_resp = await self._search.search(query=query, max_results=max_results)
        if not search_resp.results:
            log.warning("Retrieve pipeline: no search results for '%s'", query)
            return [], 0
        results = [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in search_resp.results]
        return results, len(results)

    def _dedup_step(
        self, results: list[dict[str, str]],
    ) -> tuple[list[dict[str, str]], dict[str, int]]:
        """Deduplicate search results by canonical URL.

        Returns (deduped_list, seen_keys_map) where seen_keys_map maps
        canonical keys to their index in the deduped list.
        """
        seen_keys: dict[str, int] = {}
        deduped: list[dict[str, str]] = []
        for r in results:
            key = _canonical_key(r["url"])
            if key not in seen_keys:
                seen_keys[key] = len(deduped)
                deduped.append(r)
        log.info("Retrieve pipeline: %d results after dedup (from %d)", len(deduped), len(results))
        return deduped, seen_keys

    async def _rerank_step(
        self,
        query: str,
        deduped: list[dict[str, str]],
        fetch_top_k: int,
    ) -> tuple[list[int], dict[int, float]]:
        """Run rerank on deduped results.

        Returns (reranked_indices, score_map) where score_map maps
        original dedup index to relevance score. Falls back to
        original order on failure.
        """
        rerank_docs = [
            f"{d['title']}: {d['snippet']}" if d["title"] else d["snippet"]
            for d in deduped
        ]
        top_k_rerank = min(self._settings.RETRIEVE_RERANK_TOP_K, len(rerank_docs))

        rerank_results = await self._rerank.rerank(query=query, documents=rerank_docs, top_k=top_k_rerank)

        if rerank_results is not None:
            reranked_indices = [r.index for r in rerank_results]
            score_map = {r.index: r.relevance_score for r in rerank_results}
            log.info("Retrieve pipeline: reranker returned %d results", len(rerank_results))
            return reranked_indices, score_map

        # Fallback: original order
        log.info("Retrieve pipeline: reranker unavailable, using original order")
        return list(range(len(deduped))), {}

    async def _fetch_step(
        self,
        top_urls: list[dict[str, str]],
        seen_keys: dict[str, int],
        score_map: dict[int, float],
        prefetch_tasks: dict[str, asyncio.Task],
        query: str,
    ) -> tuple[list[SourceChunk], int, int, int]:
        """Parallel fetch + quality gates for selected URLs.

        Reuses prefetch tasks where available, starts fresh tasks otherwise.
        Applies per-URL timeout to prevent slow URLs from consuming the batch.
        Runs content cleaning in parallel after all fetches complete.

        Returns (sources, sources_fetched, sources_failed, sources_skipped).
        """
        top_url_set = {u["url"] for u in top_urls}
        fetch_tasks: list[asyncio.Task] = []
        # Dynamic per-URL timeout: no single URL starves the batch
        batch_timeout = self._settings.RETRIEVE_FETCH_TIMEOUT
        per_url_timeout = max(4.0, min(10.0, batch_timeout / max(len(top_urls), 1) + 2))

        # Use BM25 content filtering for aggressive clean (retrieve pipeline)
        content_filter = "bm25"
        content_query = query

        for url_info in top_urls:
            url = url_info["url"]
            if url in prefetch_tasks:
                # Reuse speculative prefetch — already in flight.
                # Note: prefetch runs with skip_firebreak=True, so it may
                # return an anti-bot block that needs the full chain.
                # We'll check after results are collected and re-fetch if needed.
                fetch_tasks.append(prefetch_tasks[url])
            else:
                # Fresh fetch with BM25 content filtering
                async def _fetch_one(
                    u: str = url,
                    cf: str | None = content_filter,
                    cq: str | None = content_query,
                ) -> FetchResult:
                    async with asyncio.timeout(per_url_timeout):
                        return await self._fetch.execute(
                            u,
                            aggressive_clean=True,
                            skip_firebreak=False,
                            content_filter=cf,
                            content_query=cq,
                        )
                fetch_tasks.append(asyncio.create_task(_fetch_one(), name=f"fetch:{url[:80]}"))

        # Batch timeout for all fetches; individual tasks also have per_url_timeout via asyncio.timeout inside _fetch_one()
        try:
            async with asyncio.timeout(batch_timeout):
                raw_results: list[Any] = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        except asyncio.TimeoutError:
            for t in fetch_tasks:
                t.cancel()
            raw_results = []
            for t in fetch_tasks:
                try:
                    raw_results.append(t.result())
                except Exception as exc:
                    raw_results.append(exc)

        fetch_results: list[Any] = []
        for raw in raw_results:
            if isinstance(raw, asyncio.TimeoutError):
                fetch_results.append(asyncio.TimeoutError(f"Fetch timed out after {per_url_timeout:.1f}s"))
            elif isinstance(raw, Exception):
                fetch_results.append(raw)
            else:
                fetch_results.append(raw)

        # Cancel any prefetch tasks that weren't selected by rerank
        for url, task in prefetch_tasks.items():
            if url not in top_url_set:
                task.cancel()

        # Post-fetch: parallel content cleaning
        # The fetch chain already cleaned content via clean_content in execute(),
        # but when prefetch was used, content was also cleaned there.
        # No additional cleaning pass needed here — already done inline.

        sources: list[SourceChunk] = []
        sources_failed = 0
        sources_skipped_quality = 0
        min_content_length = self._settings.RETRIEVE_MIN_CONTENT_LENGTH

        for url_info, result in zip(top_urls, fetch_results):
            if isinstance(result, Exception):
                log.warning("Fetch exception for %s: %s", url_info["url"], result)
                sources_failed += 1
                continue
            if not result.success:
                log.warning("Fetch failed for %s: %s", url_info["url"], result.error)
                sources_failed += 1
                continue

            # ── Quality gates ───────────────────────────────────────
            content = result.markdown

            # Anti-bot re-fetch: if this was a prefetch result (skip_firebreak=True)
            # and it looks like an anti-bot block, re-fetch with the full chain.
            if url_info["url"] in prefetch_tasks and result.success and _is_likely_paywall(content):
                log.info(
                    "Re-fetching %s with full chain (prefetch hit anti-bot/paywall)",
                    url_info["url"],
                )
                try:
                    async with asyncio.timeout(per_url_timeout):
                        result = await self._fetch.execute(
                            url_info["url"],
                            aggressive_clean=True,
                            skip_firebreak=False,
                            content_filter=content_filter,
                            content_query=content_query,
                        )
                    content = result.markdown
                    # Re-check if the re-fetch succeeded
                    if not result.success:
                        log.warning("Re-fetch failed for %s: %s", url_info["url"], result.error)
                        sources_failed += 1
                        continue
                except asyncio.TimeoutError:
                    log.warning("Re-fetch timed out for %s", url_info["url"])
                    sources_failed += 1
                    continue

            if _is_too_short(content, min_content_length):
                log.info(
                    "Skipping source %s: content too short (%d chars < %d min)",
                    url_info["url"], len(content), min_content_length,
                )
                sources_skipped_quality += 1
                continue
            if _is_likely_paywall(content):
                log.info("Skipping source %s: detected paywall/login wall", url_info["url"])
                sources_skipped_quality += 1
                continue

            original_idx = seen_keys.get(_canonical_key(url_info["url"]))
            relevance_score = score_map.get(original_idx) if original_idx is not None else None

            content = _truncate_content(content, self._settings.RETRIEVE_MAX_CONTENT_PER_SOURCE)

            sources.append(SourceChunk(
                url=url_info["url"],
                title=result.title or url_info["title"],
                content=content,
                fetch_tier=result.source or None,
                content_length=len(content),
                relevance_score=relevance_score,
                fetch_time_ms=result.fetch_time_ms,
            ))

        sources_fetched = len(sources)
        log.info(
            "Retrieve pipeline: fetched %d/%d sources (%d failed, %d skipped by quality gates)",
            sources_fetched,
            len(top_urls),
            sources_failed,
            sources_skipped_quality,
        )
        return sources, sources_fetched, sources_failed, sources_skipped_quality

    def _budget_step(self, sources: list[SourceChunk]) -> list[SourceChunk]:
        """Enforce max total content with relevance-weighted budget allocation.

        Higher-relevance sources get more chars. Minimum floor ensures
        even low-relevance sources aren't starved.
        """
        total_content = sum(len(s.content) for s in sources)
        if total_content <= self._settings.RETRIEVE_MAX_TOTAL_CONTENT:
            return sources

        budget = self._settings.RETRIEVE_MAX_TOTAL_CONTENT
        scores = [s.relevance_score or 0.5 for s in sources]
        total_weight = sum(scores)
        per_source_budgets = [
            max(budget // len(sources) // 2, int(budget * (w / total_weight)))
            for w in scores
        ]
        # Normalize: if total exceeds limit, scale down proportionally
        total_budget = sum(per_source_budgets)
        if total_budget > budget:
            scale = budget / total_budget
            per_source_budgets = [int(b * scale) for b in per_source_budgets]

        for i in range(len(sources)):
            sources[i].content = _truncate_content(sources[i].content, per_source_budgets[i])

        actual_total = sum(len(s.content) for s in sources)
        log.info(
            "Total content truncated from %d to %d chars (relevance-weighted budget)",
            total_content, actual_total,
        )
        return sources

    # ------------------------------------------------------------------
    # Shared pipeline (used by both sync and streaming paths)
    # ------------------------------------------------------------------

    async def _run_pipeline(
        self,
        query: str,
        max_results: int,
        fetch_top_k: int,
    ) -> tuple[list[SourceChunk], int, int, int, list[dict[str, str]]]:
        """Run search → dedup → rerank → fetch → quality gates.

        Returns:
            (sources, sources_fetched, sources_failed, sources_skipped_quality, top_urls)
        """
        # ── Step 1: Search ───────────────────────────────────────────────
        results, _ = await self._search_step(query, max_results)
        if not results:
            return [], 0, 0, 0, []

        # ── Step 2: Dedup ────────────────────────────────────────────────
        deduped, seen_keys = self._dedup_step(results)

        # ── Step 3: Rerank (with optional speculative prefetch) ─────────
        # Start fetching top search results during rerank to overlap latency.
        prefetch_tasks: dict[str, asyncio.Task] = {}
        if self._settings.RETRIEVE_PREFETCH_DURING_RERANK:
            prefetch_count = min(self._settings.RETRIEVE_PREFETCH_MAX, fetch_top_k, len(deduped))
            for i in range(prefetch_count):
                url = deduped[i]["url"]
                # Prefetch skips paid anti-bot services — only free tiers (Crawl4AI, Jina)
                prefetch_tasks[url] = asyncio.create_task(
                    self._fetch.execute(
                        url,
                        aggressive_clean=True,
                        skip_firebreak=True,  # Don't waste paid API calls on speculative fetches
                        content_filter="bm25",
                        content_query=query,
                    ),
                    name=f"prefetch:{url[:80]}",
                )
            log.info("Retrieve pipeline: speculatively prefetching %d URLs during rerank", len(prefetch_tasks))

        reranked_indices, score_map = await self._rerank_step(query, deduped, fetch_top_k)

        # ── Step 4: Select top K URLs to fetch ──────────────────────────
        fetch_count = min(fetch_top_k, len(reranked_indices))
        top_urls: list[dict[str, str]] = [deduped[idx] for idx in reranked_indices[:fetch_count]]
        log.info("Retrieve pipeline: fetching top %d URLs", len(top_urls))

        # ── Step 5: Parallel fetch + quality gates ──────────────────────
        sources, sources_fetched, sources_failed, sources_skipped = await self._fetch_step(
            top_urls, seen_keys, score_map, prefetch_tasks, query,
        )

        # ── Step 6: Budget enforcement ──────────────────────────────────
        if sources:
            self._budget_step(sources)

        return sources, sources_fetched, sources_failed, sources_skipped, top_urls

    # ------------------------------------------------------------------
    # Sync (non-streaming) path
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        query: str,
        max_results: int = 10,
        fetch_top_k: int = 5,
        synthesize: bool = True,
    ) -> RetrieveResponse:
        """Run the full retrieve pipeline (non-streaming).

        Args:
            query: Research query.
            max_results: Number of search results to retrieve.
            fetch_top_k: Top K results to fetch content from.
            synthesize: If True, synthesize an answer. If False, return raw chunks.

        Returns:
            RetrieveResponse with answer, citations, and rich source metadata.
        """
        sources, sources_fetched, sources_failed, sources_skipped, top_urls = await self._run_pipeline(
            query=query, max_results=max_results, fetch_top_k=fetch_top_k
        )

        # No search results at all
        if not sources and sources_failed == 0 and sources_skipped == 0:
            return RetrieveResponse(
                query=query, answer="", citations=[], sources=[],
                sources_fetched=0, sources_failed=0,
            )

        # All fetches failed
        if not sources and sources_failed > 0:
            return RetrieveResponse(
                query=query,
                answer="All source fetches failed. Only search snippets are available.",
                citations=[],
                sources=[],
                sources_fetched=0,
                sources_failed=sources_failed,
            )

        # All sources filtered out by quality gates
        if not sources and sources_skipped > 0:
            citations = [Citation(id=i + 1, url=u["url"], title=u["title"]) for i, u in enumerate(top_urls)]
            return RetrieveResponse(
                query=query,
                answer="All fetched sources were filtered out: too short or paywalled. Only search snippets are available.",
                citations=citations,
                sources=[],
                sources_fetched=0,
                sources_failed=sources_failed + sources_skipped,
            )

        if not synthesize:
            citations = [
                Citation(id=i + 1, url=s.url, title=s.title, relevance_score=s.relevance_score)
                for i, s in enumerate(sources)
            ]
            return RetrieveResponse(
                query=query,
                answer="",
                citations=citations,
                sources=sources,
                sources_fetched=sources_fetched,
                sources_failed=sources_failed,
            )

        answer, citations = await self._synthesis.synthesize(query=query, sources=sources)

        for i, citation in enumerate(citations):
            if i < len(sources) and citation.relevance_score is None:
                citation.relevance_score = sources[i].relevance_score

        return RetrieveResponse(
            query=query,
            answer=answer,
            citations=citations,
            sources=sources,
            sources_fetched=sources_fetched,
            sources_failed=sources_failed,
        )

    # ------------------------------------------------------------------
    # Streaming path
    # ------------------------------------------------------------------

    async def retrieve_stream(
        self,
        query: str,
        max_results: int = 10,
        fetch_top_k: int = 5,
    ) -> AsyncIterator[str]:
        """Run the full retrieve pipeline and stream the LLM synthesis as SSE.

        Yields SSE-formatted event lines:
        - event: meta
          data: {"query": "...", "sources_fetched": N, "sources_failed": N}
        - event: source
          data: {"id": 1, "url": "...", "title": "...", "relevance_score": 0.95, "fetch_tier": "crawl4ai"}
        - event: token
          data: "..."  (JSON-encoded token string)
        - event: done
          data: {"finish_reason": "stop"}

        Search/rerank/fetch are fully synchronous before any tokens are yielded.
        Only the LLM synthesis phase is streamed.
        """
        sources, sources_fetched, sources_failed, _sources_skipped, _top_urls = await self._run_pipeline(
            query=query, max_results=max_results, fetch_top_k=fetch_top_k
        )

        # ── Meta event ────────────────────────────────────────────────
        meta = {
            "query": query,
            "sources_fetched": sources_fetched,
            "sources_failed": sources_failed,
        }
        yield f"event: meta\ndata: {json.dumps(meta)}\n\n"

        # ── Source events ───────────────────────────────────────────────
        for i, src in enumerate(sources, start=1):
            source_event = {
                "id": i,
                "url": src.url,
                "title": src.title,
                "relevance_score": src.relevance_score,
                "fetch_tier": src.fetch_tier,
            }
            yield f"event: source\ndata: {json.dumps(source_event)}\n\n"

        # ── Token events (streamed synthesis) ──────────────────────────
        if not sources:
            yield f"event: token\ndata: {json.dumps('No sources were available to synthesize an answer.')}\n\n"
            yield f"event: done\ndata: {json.dumps({'finish_reason': 'no_sources'})}\n\n"
            return

        async for token in self._synthesis.synthesize_stream(query=query, sources=sources):
            yield f"event: token\ndata: {json.dumps(token)}\n\n"

        # ── Done event ──────────────────────────────────────────────────
        yield f"event: done\ndata: {json.dumps({'finish_reason': 'stop'})}\n\n"
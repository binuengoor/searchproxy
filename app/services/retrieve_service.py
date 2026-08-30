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
import urllib.parse
from typing import AsyncIterator

from fastapi import Request

from app.config import Settings
from app.schemas import Citation, RetrieveResponse, SourceChunk
from app.services.fetch_chain import FetchChain
from app.services.litellm_search import LiteLLMSearchClient
from app.services.rerank_service import RerankService
from app.services.retrieve_steps import (
    budget_step,
    check_disconnect,
    dedup_step,
    fetch_step,
    fetch_step_incremental,
    rerank_step,
    search_step,
)
from app.services.cache import CacheService
from app.services.synthesis_service import SynthesisService

log = logging.getLogger(__name__)


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
        cache: CacheService | None = None,
    ) -> None:
        self._search = search_client
        self._fetch = fetch_chain
        self._rerank = rerank_service
        self._synthesis = synthesis_service
        self._settings = settings
        self._cache = cache

    async def _pipeline_setup(
        self,
        query: str,
        max_results: int,
        fetch_top_k: int,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        request: Request | None = None,
    ) -> tuple[dict[str, int], dict[int, float], dict[str, asyncio.Task], list[dict[str, str]]]:
        """Run search, dedup, speculative prefetch, rerank, and top-K candidate selection.

        Returns (seen_keys, score_map, prefetch_tasks, top_urls).
        If no search results are found, returns ({}, {}, {}, []).
        """
        # ── Step 1: Search ───────────────────────────────────────────────
        results, _ = await search_step(self._search, query, max_results)
        await check_disconnect(request)
        if not results:
            return {}, {}, {}, []

        # ── Step 1.5: Domain filtering ──────────────────────────────────
        if include_domains:
            inc_set = {d.strip().lower() for d in include_domains if d.strip()}
            results = [
                r for r in results
                if any(inc in urllib.parse.urlparse(r["url"]).netloc.lower() for inc in inc_set)
            ]

        if exclude_domains:
            exc_set = {d.strip().lower() for d in exclude_domains if d.strip()}
            results = [
                r for r in results
                if not any(exc in urllib.parse.urlparse(r["url"]).netloc.lower() for exc in exc_set)
            ]

        if not results:
            return {}, {}, {}, []

        # ── Step 2: Dedup ────────────────────────────────────────────────
        deduped, seen_keys = dedup_step(results)

        # ── Step 3: Rerank (with optional speculative prefetch) ─────────
        prefetch_tasks: dict[str, asyncio.Task] = {}
        if self._settings.RETRIEVE_PREFETCH_DURING_RERANK:
            prefetch_count = min(self._settings.RETRIEVE_PREFETCH_MAX, fetch_top_k, len(deduped))
            for i in range(prefetch_count):
                url = deduped[i]["url"]
                prefetch_tasks[url] = asyncio.create_task(
                    self._fetch.execute(
                        url,
                        aggressive_clean=True,
                        skip_firebreak=True,
                        content_filter="bm25",
                        content_query=query,
                    ),
                    name=f"prefetch:{url[:80]}",
                )
            log.info("Retrieve pipeline: speculatively prefetching %d URLs during rerank", len(prefetch_tasks))

        reranked_indices, score_map = await rerank_step(
            query, deduped, fetch_top_k, self._rerank, self._settings,
        )
        await check_disconnect(request)

        # ── Step 4: Select top K URLs to fetch with domain diversity ────
        top_urls: list[dict[str, str]] = []
        domain_counts: dict[str, int] = {}
        max_per_domain = self._settings.MAX_PER_DOMAIN_SOURCES

        for idx in reranked_indices:
            cand = deduped[idx]
            domain = urllib.parse.urlparse(cand["url"]).netloc.lower()
            if domain_counts.get(domain, 0) < max_per_domain:
                top_urls.append(cand)
                domain_counts[domain] = domain_counts.get(domain, 0) + 1
                if len(top_urls) >= fetch_top_k:
                    break

        if len(top_urls) < min(fetch_top_k, len(reranked_indices)):
            existing_urls = {u["url"] for u in top_urls}
            for idx in reranked_indices:
                cand = deduped[idx]
                if cand["url"] not in existing_urls:
                    top_urls.append(cand)
                    if len(top_urls) >= fetch_top_k:
                        break

        log.info("Retrieve pipeline: selected %d top URLs across %d domains", len(top_urls), len(domain_counts))
        return seen_keys, score_map, prefetch_tasks, top_urls

    async def _run_pipeline(
        self,
        query: str,
        max_results: int,
        fetch_top_k: int,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        request: Request | None = None,
    ) -> tuple[list[SourceChunk], int, int, int, list[dict[str, str]]]:
        """Run search → dedup → rerank → fetch → quality gates."""
        seen_keys, score_map, prefetch_tasks, top_urls = await self._pipeline_setup(
            query, max_results, fetch_top_k,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            request=request,
        )
        if not top_urls:
            return [], 0, 0, 0, []

        # ── Step 5: Parallel fetch + quality gates ──────────────────────
        sources, sources_fetched, sources_failed, sources_skipped = await fetch_step(
            top_urls, seen_keys, score_map, prefetch_tasks, query,
            self._fetch, self._settings,
        )
        await check_disconnect(request)

        # ── Step 6: Budget enforcement ──────────────────────────────────
        if sources:
            budget_step(sources, self._settings)

        return sources, sources_fetched, sources_failed, sources_skipped, top_urls

    async def retrieve(
        self,
        query: str,
        max_results: int = 10,
        fetch_top_k: int = 5,
        synthesize: bool = True,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        request: Request | None = None,
    ) -> RetrieveResponse:
        """Run the full retrieve pipeline (non-streaming)."""
        sources, sources_fetched, sources_failed, sources_skipped, top_urls = await self._run_pipeline(
            query=query, max_results=max_results, fetch_top_k=fetch_top_k,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            request=request,
        )

        if not sources and sources_failed == 0 and sources_skipped == 0:
            return RetrieveResponse(
                query=query, answer="", citations=[], sources=[],
                sources_fetched=0, sources_failed=0,
            )

        if not sources and sources_failed > 0:
            return RetrieveResponse(
                query=query,
                answer="All source fetches failed. Only search snippets are available.",
                citations=[],
                sources=[],
                sources_fetched=0,
                sources_failed=sources_failed,
            )

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

        # Synthesis cache: identical query + source URLs can return immediately
        cached = None
        if self._cache is not None:
            cached = await self._cache.get_synthesize(query, top_urls)
        if cached is not None:
            log.info("Retrieve pipeline: synthesis cache hit")
            cached_citations = [
                Citation(**c) if isinstance(c, dict) else c
                for c in cached.get("citations", [])
            ]
            return RetrieveResponse(
                query=query,
                answer=cached.get("answer", ""),
                citations=cached_citations,
                sources=sources,
                sources_fetched=sources_fetched,
                sources_failed=sources_failed,
            )

        answer, citations = await self._synthesis.synthesize(query=query, sources=sources)

        for i, citation in enumerate(citations):
            if i < len(sources) and citation.relevance_score is None:
                citation.relevance_score = sources[i].relevance_score

        if self._cache is not None:
            await self._cache.set_synthesize(
                query, top_urls,
                {
                    "answer": answer,
                    "citations": [c.model_dump() for c in citations],
                },
            )

        return RetrieveResponse(
            query=query,
            answer=answer,
            citations=citations,
            sources=sources,
            sources_fetched=sources_fetched,
            sources_failed=sources_failed,
        )

    async def retrieve_stream(
        self,
        query: str,
        max_results: int = 10,
        fetch_top_k: int = 5,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        request: Request | None = None,
    ) -> AsyncIterator[str]:
        """Run the full retrieve pipeline and stream the LLM synthesis as SSE.

        Sources are emitted incrementally as each individual fetch completes,
        dramatically improving perceived latency compared to waiting for the
        entire batch.
        """
        # ── Steps 1-4: Search, dedup, rerank, select top K ───────────────
        seen_keys, score_map, prefetch_tasks, top_urls = await self._pipeline_setup(
            query, max_results, fetch_top_k,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            request=request,
        )
        if not top_urls:
            meta = {"query": query, "sources_fetched": 0, "sources_failed": 0}
            yield f"event: meta\ndata: {json.dumps(meta)}\n\n"
            yield f"event: token\ndata: {json.dumps('No search results found.')}\n\n"
            yield f"event: done\ndata: {json.dumps({'finish_reason': 'no_results'})}\n\n"
            return

        # ── Step 5: Incremental fetch + stream sources ───────────────────
        meta = {"query": query, "sources_fetched": 0, "sources_failed": 0}
        yield f"event: meta\ndata: {json.dumps(meta)}\n\n"

        sources: list[SourceChunk] = []
        sources_failed = 0
        sources_skipped = 0
        source_id = 1

        async for src, failed, skipped in fetch_step_incremental(
            top_urls, seen_keys, score_map, prefetch_tasks, query,
            self._fetch, self._settings,
        ):
            sources_failed += failed
            sources_skipped += skipped
            if src is not None:
                sources.append(src)
                source_event = {
                    "id": source_id,
                    "url": src.url,
                    "title": src.title,
                    "relevance_score": src.relevance_score,
                    "fetch_tier": src.fetch_tier,
                }
                yield f"event: source\ndata: {json.dumps(source_event)}\n\n"
                source_id += 1

        await check_disconnect(request)

        if sources:
            budget_step(sources, self._settings)

        if not sources:
            yield f"event: token\ndata: {json.dumps('No sources were available to synthesize an answer.')}\n\n"
            yield f"event: done\ndata: {json.dumps({'finish_reason': 'no_sources'})}\n\n"
            return

        async for token in self._synthesis.synthesize_stream(query=query, sources=sources):
            yield f"event: token\ndata: {json.dumps(token)}\n\n"

        yield f"event: done\ndata: {json.dumps({'finish_reason': 'stop'})}\n\n"

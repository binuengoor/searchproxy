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
    rerank_step,
    search_step,
)
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
    ) -> None:
        self._search = search_client
        self._fetch = fetch_chain
        self._rerank = rerank_service
        self._synthesis = synthesis_service
        self._settings = settings

    async def _run_pipeline(
        self,
        query: str,
        max_results: int,
        fetch_top_k: int,
        request: Request | None = None,
    ) -> tuple[list[SourceChunk], int, int, int, list[dict[str, str]]]:
        """Run search → dedup → rerank → fetch → quality gates."""
        # ── Step 1: Search ───────────────────────────────────────────────
        results, _ = await search_step(self._search, query, max_results)
        await check_disconnect(request)
        if not results:
            return [], 0, 0, 0, []

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

        # ── Step 4: Select top K URLs to fetch ──────────────────────────
        fetch_count = min(fetch_top_k, len(reranked_indices))
        top_urls: list[dict[str, str]] = [deduped[idx] for idx in reranked_indices[:fetch_count]]
        log.info("Retrieve pipeline: fetching top %d URLs", len(top_urls))

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
        request: Request | None = None,
    ) -> RetrieveResponse:
        """Run the full retrieve pipeline (non-streaming)."""
        sources, sources_fetched, sources_failed, sources_skipped, top_urls = await self._run_pipeline(
            query=query, max_results=max_results, fetch_top_k=fetch_top_k, request=request,
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

    async def retrieve_stream(
        self,
        query: str,
        max_results: int = 10,
        fetch_top_k: int = 5,
        request: Request | None = None,
    ) -> AsyncIterator[str]:
        """Run the full retrieve pipeline and stream the LLM synthesis as SSE."""
        sources, sources_fetched, sources_failed, _sources_skipped, _top_urls = await self._run_pipeline(
            query=query, max_results=max_results, fetch_top_k=fetch_top_k, request=request,
        )

        meta = {
            "query": query,
            "sources_fetched": sources_fetched,
            "sources_failed": sources_failed,
        }
        yield f"event: meta\ndata: {json.dumps(meta)}\n\n"

        for i, src in enumerate(sources, start=1):
            source_event = {
                "id": i,
                "url": src.url,
                "title": src.title,
                "relevance_score": src.relevance_score,
                "fetch_tier": src.fetch_tier,
            }
            yield f"event: source\ndata: {json.dumps(source_event)}\n\n"

        if not sources:
            yield f"event: token\ndata: {json.dumps('No sources were available to synthesize an answer.')}\n\n"
            yield f"event: done\ndata: {json.dumps({'finish_reason': 'no_sources'})}\n\n"
            return

        async for token in self._synthesis.synthesize_stream(query=query, sources=sources):
            yield f"event: token\ndata: {json.dumps(token)}\n\n"

        yield f"event: done\ndata: {json.dumps({'finish_reason': 'stop'})}\n\n"

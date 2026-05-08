"""Retrieve orchestrator — composes search ➜ rerank ➜ fetch ➜ synthesize.

The /v1/retrieve endpoint's core pipeline. Each step is a separate service
with its own client, making the orchestration easy to test and extend.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.schemas import Citation, RetrieveResponse, SourceChunk
from app.services.fetch_chain import FetchChain
from app.services.litellm_search import LiteLLMSearchClient
from app.services.rerank_service import RerankService
from app.services.synthesis_service import SynthesisService

log = logging.getLogger(__name__)

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
    """Truncate content to max_chars, keeping the beginning."""
    if len(content) <= max_chars:
        return content
    return content[:max_chars]


class RetrieveService:
    """Orchestrates: search → dedup → rerank → parallel fetch → chunk → synthesize.

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

    async def retrieve(
        self,
        query: str,
        max_results: int = 10,
        fetch_top_k: int = 5,
        synthesize: bool = True,
    ) -> RetrieveResponse:
        """Run the full retrieve pipeline.

        Args:
            query: Research query.
            max_results: Number of search results to retrieve.
            fetch_top_k: Top K results to fetch content from.
            synthesize: If True, synthesize an answer. If False, return raw chunks.

        Returns:
            RetrieveResponse with answer, citations, and source info.
        """
        # ── Step 1: Search ───────────────────────────────────────────────
        log.info("Retrieve pipeline: search for '%s' (max_results=%d)", query, max_results)
        search_resp = await self._search.search(query=query, max_results=max_results)

        if not search_resp.results:
            log.warning("Retrieve pipeline: no search results for '%s'", query)
            return RetrieveResponse(query=query, answer="", citations=[], sources=[], sources_fetched=0, sources_failed=0)

        # ── Step 2: Dedup by canonical URL ──────────────────────────────
        seen_keys: dict[str, int] = {}
        deduped: list[dict[str, str]] = []
        for r in search_resp.results:
            key = _canonical_key(r.url)
            if key not in seen_keys:
                seen_keys[key] = len(deduped)
                deduped.append({"title": r.title, "url": r.url, "snippet": r.snippet})

        log.info("Retrieve pipeline: %d results after dedup (from %d)", len(deduped), len(search_resp.results))

        # ── Step 3: Rerank ──────────────────────────────────────────────
        # Build document strings for the reranker: "title: snippet"
        rerank_docs = [f"{d['title']}: {d['snippet']}" if d['title'] else d['snippet'] for d in deduped]
        top_k_rerank = min(self._settings.RETRIEVE_RERANK_TOP_K, len(rerank_docs))

        reranked_indices: list[int] | None = None
        rerank_results = await self._rerank.rerank(query=query, documents=rerank_docs, top_k=top_k_rerank)

        if rerank_results is not None:
            # Rerank succeeded — use its ordering
            reranked_indices = [r.index for r in rerank_results]
            log.info("Retrieve pipeline: reranker returned %d results", len(rerank_results))
        else:
            # Rerank failed — fall back to original search order
            reranked_indices = list(range(len(deduped)))
            log.info("Retrieve pipeline: reranker unavailable, using original order")

        # ── Step 4: Select top K URLs to fetch ──────────────────────────
        fetch_count = min(fetch_top_k, len(reranked_indices))
        top_urls: list[dict[str, str]] = []
        for idx in reranked_indices[:fetch_count]:
            top_urls.append(deduped[idx])

        log.info("Retrieve pipeline: fetching top %d URLs", len(top_urls))

        # ── Step 5: Parallel fetch ──────────────────────────────────────
        fetch_tasks = [self._fetch.execute(u["url"]) for u in top_urls]
        fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        sources: list[SourceChunk] = []
        sources_failed = 0

        for url_info, result in zip(top_urls, fetch_results):
            if isinstance(result, Exception):
                log.warning("Fetch exception for %s: %s", url_info["url"], result)
                sources_failed += 1
                continue
            if not result.success:
                log.warning("Fetch failed for %s: %s", url_info["url"], result.error)
                sources_failed += 1
                continue
            # Truncate per-source content
            content = _truncate_content(result.markdown, self._settings.RETRIEVE_MAX_CONTENT_PER_SOURCE)
            sources.append(SourceChunk(
                url=url_info["url"],
                title=result.title or url_info["title"],
                content=content,
            ))

        sources_fetched = len(sources)
        log.info(
            "Retrieve pipeline: fetched %d/%d sources (%d failed)",
            sources_fetched,
            len(top_urls),
            sources_failed,
        )

        if not sources and sources_failed == len(top_urls):
            # All fetches failed — still return search metadata
            log.warning("Retrieve pipeline: all fetches failed for '%s'", query)
            citations = [Citation(id=i + 1, url=u["url"], title=u["title"]) for i, u in enumerate(top_urls)]
            return RetrieveResponse(
                query=query,
                answer="All source fetches failed. Only search snippets are available.",
                citations=citations,
                sources=[],
                sources_fetched=0,
                sources_failed=sources_failed,
            )

        # ── Step 6: Enforce max total content ────────────────────────────
        total_content = sum(len(s.content) for s in sources)
        if total_content > self._settings.RETRIEVE_MAX_TOTAL_CONTENT:
            # Truncate proportionally from each source
            budget = self._settings.RETRIEVE_MAX_TOTAL_CONTENT
            per_source = budget // len(sources)
            for i in range(len(sources)):
                sources[i].content = _truncate_content(sources[i].content, per_source)
            log.info("Total content truncated from %d to ~%d chars", total_content, budget)

        # ── Step 7: Synthesize (or skip) ────────────────────────────────
        if not synthesize:
            citations = [Citation(id=i + 1, url=s.url, title=s.title) for i, s in enumerate(sources)]
            return RetrieveResponse(
                query=query,
                answer="",
                citations=citations,
                sources=sources,
                sources_fetched=sources_fetched,
                sources_failed=sources_failed,
            )

        answer, citations = await self._synthesis.synthesize(query=query, sources=sources)

        return RetrieveResponse(
            query=query,
            answer=answer,
            citations=citations,
            sources=sources,
            sources_fetched=sources_fetched,
            sources_failed=sources_failed,
        )
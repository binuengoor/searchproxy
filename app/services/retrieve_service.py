"""Retrieve orchestrator — composes search ➜ rerank ➜ fetch ➜ synthesize.

The /v1/retrieve endpoint's core pipeline. Each step is a separate service
with its own client, making the orchestration easy to test and extend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.schemas import Citation, RetrieveResponse, SourceChunk
from app.services.fetch_chain import FetchChain
from app.services.litellm_search import LiteLLMSearchClient
from app.services.rerank_service import RerankService
from app.services.synthesis_service import SynthesisService

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
    "access denied",
]
_PAYWALL_RE = re.compile(r"(?:" + "|".join(re.escape(p) for p in _PAYWALL_PATTERNS) + r")", re.IGNORECASE)


def _is_likely_paywall(content: str) -> bool:
    """Detect paywall/login-wall pages by common phrases."""
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

    # ------------------------------------------------------------------
    # Shared pipeline steps (used by both sync and streaming paths)
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
        log.info("Retrieve pipeline: search for '%s' (max_results=%d)", query, max_results)
        search_resp = await self._search.search(query=query, max_results=max_results)

        if not search_resp.results:
            log.warning("Retrieve pipeline: no search results for '%s'", query)
            return [], 0, 0, 0, []

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
        rerank_docs = [f"{d['title']}: {d['snippet']}" if d['title'] else d['snippet'] for d in deduped]
        top_k_rerank = min(self._settings.RETRIEVE_RERANK_TOP_K, len(rerank_docs))

        reranked_indices: list[int] | None = None
        rerank_results = await self._rerank.rerank(query=query, documents=rerank_docs, top_k=top_k_rerank)

        rerank_score_by_idx: dict[int, float] = {}
        if rerank_results is not None:
            reranked_indices = [r.index for r in rerank_results]
            for r in rerank_results:
                rerank_score_by_idx[r.index] = r.relevance_score
            log.info("Retrieve pipeline: reranker returned %d results", len(rerank_results))
        else:
            reranked_indices = list(range(len(deduped)))
            log.info("Retrieve pipeline: reranker unavailable, using original order")

        # ── Step 4: Select top K URLs to fetch ──────────────────────────
        fetch_count = min(fetch_top_k, len(reranked_indices))
        top_urls: list[dict[str, str]] = []
        for idx in reranked_indices[:fetch_count]:
            top_urls.append(deduped[idx])

        log.info("Retrieve pipeline: fetching top %d URLs", len(top_urls))

        # ── Step 5: Parallel fetch ──────────────────────────────────────
        fetch_tasks = [self._fetch.execute(u["url"], aggressive_clean=True) for u in top_urls]
        fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        sources: list[SourceChunk] = []
        sources_failed = 0
        sources_skipped_quality = 0
        min_content_length = getattr(self._settings, "RETRIEVE_MIN_CONTENT_LENGTH", 300)

        for url_info, result in zip(top_urls, fetch_results):
            if isinstance(result, Exception):
                log.warning("Fetch exception for %s: %s", url_info["url"], result)
                sources_failed += 1
                continue
            if not result.success:
                log.warning("Fetch failed for %s: %s", url_info["url"], result.error)
                sources_failed += 1
                continue

            # ── Step 5a: Quality gates ───────────────────────────────────
            content = result.markdown
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
            relevance_score = rerank_score_by_idx.get(original_idx) if original_idx is not None else None

            raw_content_length = len(content)
            content = _truncate_content(content, self._settings.RETRIEVE_MAX_CONTENT_PER_SOURCE)

            sources.append(SourceChunk(
                url=url_info["url"],
                title=result.title or url_info["title"],
                content=content,
                fetch_tier=result.source or None,
                content_length=raw_content_length,
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

        # ── Step 6: Enforce max total content ────────────────────────────
        total_content = sum(len(s.content) for s in sources)
        if total_content > self._settings.RETRIEVE_MAX_TOTAL_CONTENT:
            budget = self._settings.RETRIEVE_MAX_TOTAL_CONTENT
            per_source = budget // len(sources)
            for i in range(len(sources)):
                sources[i].content = _truncate_content(sources[i].content, per_source)
            log.info("Total content truncated from %d to ~%d chars", total_content, budget)

        return sources, sources_fetched, sources_failed, sources_skipped_quality, top_urls

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
"""Native Python 2-Hop Deep Research Service.

Replaces external Node.js/Vane dependency with a lightweight, asynchronous
multi-step research pipeline:
1. Query Decomposition: Generates 2-3 focused sub-queries.
2. Parallel Sub-Search: Dispatches parallel searches and pools results.
3. Neural Dedup & Rerank: Merges and scores all candidates against the primary query.
4. Tiered Multi-Fetch: Fetches top diverse sources via Crawl4AI/Byparr/Tika.
5. In-Depth Structured Synthesis: Generates an extensive, cited research report.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

from fastapi import Request
import httpx

from app.config import Settings
from app.schemas import Citation, RetrieveResponse, SourceChunk
from app.services.fetch_chain import FetchChain
from app.services.search import SearchRouter
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

_DECOMPOSE_SYSTEM_PROMPT = """\
You are an expert research query planner.
Given a complex user topic or question, break it down into 2 to 3 distinct, highly effective search queries to gather comprehensive information from different angles.

Output ONLY a JSON array of search query strings. Example:
["query 1", "query 2", "query 3"]
"""

_DEEP_SYNTHESIS_SYSTEM_PROMPT = """\
You are a senior research analyst. You have been provided with comprehensive, multi-source intelligence from parallel web searches.
Your goal is to write an exhaustive, structured, highly factual research report answering the user's inquiry.

## Report Structure
1. # Executive Summary — High-level synthesis of findings and core answer.
2. ## Comprehensive Analysis — Detailed breakdown organized into logical thematic sections. Include background context, mechanics, data points, and comparisons.
3. ## Key Takeaways & Implications — Bulleted summary of critical conclusions.
4. ## Source Coverage & Limitations (if applicable) — Any gaps or conflicting evidence in the sources.

## Strict Citation Rules
- Cite every factual claim, number, and statement with inline brackets: [1], [2], [1][3].
- Do not fabricate or speculate beyond the provided sources.
- Group and contrast differing perspectives from sources where relevant.
"""


class DeepResearchService:
    """Orchestrates 2-hop deep research with parallel sub-query expansion."""

    def __init__(
        self,
        search_client: SearchRouter,
        rerank_service: RerankService,
        fetch_chain: FetchChain,
        synthesis_service: SynthesisService,
        settings: Settings,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._search = search_client
        self._rerank = rerank_service
        self._fetch = fetch_chain
        self._synthesis = synthesis_service
        self._settings = settings
        self._http = http_client

    async def decompose_query(self, query: str) -> list[str]:
        """Generate 2-3 sub-queries for broad multi-angle coverage."""
        sub_queries = [query]
        if not self._settings.LLM_CHAT_URL:
            return sub_queries

        try:
            payload = {
                "model": self._settings.LLM_CHAT_MODEL,
                "messages": [
                    {"role": "system", "content": _DECOMPOSE_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Topic: {query}"},
                ],
                "temperature": 0.2,
                "max_tokens": 200,
            }
            resp = await self._http.post(
                self._settings.LLM_CHAT_URL,
                json=payload,
                headers={"Authorization": f"Bearer {self._settings.LLM_API_KEY}"} if self._settings.LLM_API_KEY else {},
                timeout=10.0,
            )
            if resp.status_code == 200:
                raw_text = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                # Parse JSON array
                start = raw_text.find("[")
                end = raw_text.rfind("]")
                if start != -1 and end != -1:
                    parsed = json.loads(raw_text[start : end + 1])
                    if isinstance(parsed, list) and parsed:
                        cleaned = [str(q).strip() for q in parsed if str(q).strip()]
                        if cleaned:
                            log.info("Query decomposed into %d sub-queries: %s", len(cleaned), cleaned)
                            return cleaned
        except Exception as exc:
            log.warning("Query decomposition failed for '%s': %s (using primary query)", query, exc)

        return sub_queries

    async def research(
        self,
        query: str,
        max_sub_queries: int = 3,
        fetch_top_k: int = 8,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        request: Request | None = None,
    ) -> RetrieveResponse:
        """Execute full 2-hop deep research pipeline."""
        start_time = time.perf_counter()
        log.info("Starting Deep Research for '%s'", query)

        # ── Hop 1: Sub-query expansion ───────────────────────────────────
        sub_queries = await self.decompose_query(query)
        sub_queries = sub_queries[:max_sub_queries]
        if query not in sub_queries:
            sub_queries.insert(0, query)

        await check_disconnect(request)

        # ── Hop 2: Parallel search across all sub-queries ────────────────
        search_tasks = [search_step(self._search, sq, 8) for sq in sub_queries]
        search_results_lists = await asyncio.gather(*search_tasks, return_exceptions=True)

        combined_results: list[dict[str, Any]] = []
        for res in search_results_lists:
            if isinstance(res, tuple) and res[0]:
                combined_results.extend(res[0])

        if not combined_results:
            log.warning("Deep Research found 0 search results across all sub-queries")
            return RetrieveResponse(
                query=query, answer="No relevant information found across deep research sources.",
                citations=[], sources=[], sources_fetched=0, sources_failed=0,
            )

        # ── Deduplication & Domain Filtering ────────────────────────────
        deduped, seen_keys = dedup_step(combined_results)
        if include_domains:
            inc_set = {d.strip().lower() for d in include_domains if d.strip()}
            deduped = [d for d in deduped if any(inc in d["url"].lower() for inc in inc_set)]

        if exclude_domains:
            exc_set = {d.strip().lower() for d in exclude_domains if d.strip()}
            deduped = [d for d in deduped if not any(exc in d["url"].lower() for exc in exc_set)]

        if not deduped:
            return RetrieveResponse(
                query=query, answer="All deep research search results were excluded by domain filters.",
                citations=[], sources=[], sources_fetched=0, sources_failed=0,
            )

        # ── Neural Reranking against Primary Query ───────────────────────
        reranked_indices, score_map = await rerank_step(
            query, deduped, fetch_top_k, self._rerank, self._settings,
        )
        await check_disconnect(request)

        # ── Top-K Selection with Domain Diversity ────────────────────────
        top_urls: list[dict[str, str]] = []
        domain_counts: dict[str, int] = {}
        max_per_domain = self._settings.MAX_PER_DOMAIN_SOURCES

        for idx in reranked_indices:
            cand = deduped[idx]
            from urllib.parse import urlparse
            domain = urlparse(cand["url"]).netloc.lower()
            if domain_counts.get(domain, 0) < max_per_domain:
                top_urls.append(cand)
                domain_counts[domain] = domain_counts.get(domain, 0) + 1
                if len(top_urls) >= fetch_top_k:
                    break

        if len(top_urls) < min(fetch_top_k, len(reranked_indices)):
            existing = {u["url"] for u in top_urls}
            for idx in reranked_indices:
                cand = deduped[idx]
                if cand["url"] not in existing:
                    top_urls.append(cand)
                    if len(top_urls) >= fetch_top_k:
                        break

        # ── Parallel Extraction via FetchChain (Crawl4AI/Byparr/Tika) ────
        prefetch_tasks: dict[str, asyncio.Task] = {}
        sources, sources_fetched, sources_failed, sources_skipped = await fetch_step(
            top_urls, seen_keys, score_map, prefetch_tasks, query,
            self._fetch, self._settings,
        )
        await check_disconnect(request)

        if not sources:
            return RetrieveResponse(
                query=query,
                answer="Failed to extract readable content from deep research candidate sources.",
                citations=[], sources=[], sources_fetched=0, sources_failed=sources_failed,
            )

        budget_step(sources, self._settings)

        # ── Deep Report Synthesis ────────────────────────────────────────
        report_text, citations = await self._synthesize_report(query, sources)
        log.info("Deep Research completed in %.2fs (%d sources, %d citations)", time.perf_counter() - start_time, len(sources), len(citations))

        return RetrieveResponse(
            query=query,
            answer=report_text,
            citations=citations,
            sources=sources,
            sources_fetched=sources_fetched,
            sources_failed=sources_failed,
        )

    async def _synthesize_report(self, query: str, sources: list[SourceChunk]) -> tuple[str, list[Citation]]:
        """Call LLM with deep research prompt structure."""
        if not self._settings.LLM_CHAT_URL:
            # Fallback
            from app.services.synthesis_service import _fallback_answer, _extract_citations
            return _fallback_answer(sources), _extract_citations(sources)

        parts = [f"Research Query: {query}\n\nSources:\n"]
        for i, src in enumerate(sources, start=1):
            title_line = f"  Title: {src.title}\n" if src.title else ""
            parts.append(f"[{i}] URL: {src.url}\n{title_line}  Content:\n{src.content}\n")
        user_content = "\n".join(parts)

        payload = {
            "model": self._settings.LLM_CHAT_MODEL,
            "messages": [
                {"role": "system", "content": _DEEP_SYNTHESIS_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
            "max_tokens": max(self._settings.SYNTHESIS_MAX_TOKENS, 3000),
        }

        try:
            resp = await self._http.post(
                self._settings.LLM_CHAT_URL,
                json=payload,
                headers={"Authorization": f"Bearer {self._settings.LLM_API_KEY}"} if self._settings.LLM_API_KEY else {},
                timeout=self._settings.SYNTHESIS_TIMEOUT,
            )
            if resp.status_code == 200:
                answer = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                citations = [
                    Citation(id=i + 1, url=s.url, title=s.title, relevance_score=s.relevance_score)
                    for i, s in enumerate(sources)
                ]
                return answer, citations
        except Exception as exc:
            log.warning("Deep synthesis call failed: %s", exc)

        from app.services.synthesis_service import _fallback_answer, _extract_citations
        return _fallback_answer(sources), _extract_citations(sources)

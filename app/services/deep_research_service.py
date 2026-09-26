"""Native Python 2-Hop Adaptive Deep Research Service with SSE Progress Streaming.

Orchestrates an autonomous multi-step research loop:
1. Query Planning: Decomposes query into 2-3 targeted sub-queries.
2. Hop 1: Parallel search, rerank, and content extraction across initial pools.
3. Reflection & Gap Analysis: LLM identifies missing facts, contradictions, or unverified claims.
4. Hop 2: Targeted searches and content extraction for gap queries.
5. Final Synthesis: Exhaustive cited report with post-synthesis citation verification.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx
from fastapi import Request

from app.config import Settings
from app.schemas import Citation, RetrieveResponse, SourceChunk
from app.services.fetch_chain import FetchChain
from app.services.rerank_service import RerankService
from app.services.retrieve_steps import (
    _PAYWALL_RE,
    budget_step,
    canonical_key,
    check_disconnect,
    dedup_step,
    fetch_step,
    rerank_step,
    search_step,
    truncate_content,
    verify_citations_step,
)
from app.services.search import SearchRouter
from app.services.synthesis_service import SynthesisService, _fallback_answer

log = logging.getLogger(__name__)

_DECOMPOSE_SYSTEM_PROMPT = """\
You are an expert research query planner.
Given a complex user topic or question, break it down into 2 to 3 distinct, \
highly effective search queries to gather comprehensive information from different angles.

Output ONLY a JSON array of search query strings. Example:
["query 1", "query 2", "query 3"]
"""

_GAP_ANALYSIS_SYSTEM_PROMPT = """\
You are a senior research analyst evaluating interim findings.
Analyze the user's research query and the source excerpts collected in Hop 1.
Identify missing angles, unverified claims, contradictions, or critical information gaps.
Generate 1 to 2 targeted, highly specific follow-up search queries to fill those gaps.

Output ONLY a JSON array of search query strings. Example:
["targeted gap query 1", "targeted gap query 2"]
If the Hop 1 findings are already comprehensive and sufficient, output an empty array: []
"""

_DEEP_SYNTHESIS_SYSTEM_PROMPT = """\
You are a senior research analyst. You have been provided with comprehensive, \
multi-source intelligence from web searches.
Your goal is to write an exhaustive, structured, highly factual research report \
answering the user's inquiry.

## Report Structure
1. # Executive Summary — High-level synthesis of findings and core answer.
2. ## Comprehensive Analysis — Detailed breakdown organized into logical thematic sections. \
Include background context, mechanics, data points, and comparisons.
3. ## Key Takeaways & Implications — Bulleted summary of critical conclusions.
4. ## Source Coverage & Limitations — Any gaps or conflicting evidence in sources.

## Strict Citation Rules
- Cite every factual claim, number, and statement with inline brackets: [1], [2], [1][3].
- Do not fabricate or speculate beyond the provided sources.
- Group and contrast differing perspectives from sources where relevant.
"""


class DeepResearchService:
    """Orchestrates 2-hop adaptive deep research with reflection and gap analysis."""

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
            headers = (
                {"Authorization": f"Bearer {self._settings.LLM_API_KEY}"}
                if self._settings.LLM_API_KEY
                else {}
            )
            resp = await self._http.post(
                self._settings.LLM_CHAT_URL,
                json=payload,
                headers=headers,
                timeout=10.0,
            )
            if resp.status_code == 200:
                raw_text = (
                    resp.json()
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
                start = raw_text.find("[")
                end = raw_text.rfind("]")
                if start != -1 and end != -1:
                    parsed = json.loads(raw_text[start : end + 1])
                    if isinstance(parsed, list) and parsed:
                        cleaned = [str(q).strip() for q in parsed if str(q).strip()]
                        if cleaned:
                            log.info(
                                "Query decomposed into %d sub-queries: %s",
                                len(cleaned),
                                cleaned,
                            )
                            return cleaned
        except Exception as exc:
            log.warning("Query decomposition failed for '%s': %s (using primary query)", query, exc)

        return sub_queries

    async def gap_analysis(self, query: str, hop1_sources: list[SourceChunk]) -> list[str]:
        """Identify missing information or contradictions and generate 1-2 gap queries."""
        if not self._settings.LLM_CHAT_URL or not hop1_sources:
            return []

        try:
            excerpts = []
            for i, src in enumerate(hop1_sources[:6], start=1):
                excerpts.append(f"[{i}] {src.title}\n{src.content[:400]}")
            context = "\n\n".join(excerpts)
            user_content = f"Research Query: {query}\n\nHop 1 Findings:\n{context}"

            payload = {
                "model": self._settings.LLM_CHAT_MODEL,
                "messages": [
                    {"role": "system", "content": _GAP_ANALYSIS_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.2,
                "max_tokens": 200,
            }
            headers = (
                {"Authorization": f"Bearer {self._settings.LLM_API_KEY}"}
                if self._settings.LLM_API_KEY
                else {}
            )
            resp = await self._http.post(
                self._settings.LLM_CHAT_URL,
                json=payload,
                headers=headers,
                timeout=10.0,
            )
            if resp.status_code == 200:
                raw_text = (
                    resp.json()
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
                start = raw_text.find("[")
                end = raw_text.rfind("]")
                if start != -1 and end != -1:
                    parsed = json.loads(raw_text[start : end + 1])
                    if isinstance(parsed, list):
                        cleaned = [str(q).strip() for q in parsed if str(q).strip()]
                        log.info("Gap analysis generated %d queries: %s", len(cleaned), cleaned)
                        return cleaned[:2]
        except Exception as exc:
            log.warning("Gap analysis failed for '%s': %s", query, exc)

        return []

    async def _fetch_candidates(
        self,
        cands: list[dict[str, str]],
        query: str,
        seen_keys: dict[str, int],
        score_map: dict[int, float],
    ) -> tuple[list[SourceChunk], int, int]:
        """Fetch candidates using instant text bypass where available."""
        instant_sources: dict[str, SourceChunk] = {}
        urls_to_fetch: list[dict[str, str]] = []
        min_len = self._settings.RETRIEVE_MIN_CONTENT_LENGTH
        max_chars = self._settings.RETRIEVE_MAX_CONTENT_PER_SOURCE

        for c in cands:
            raw_text = c.get("text") or ""
            if len(raw_text) >= min_len and not bool(_PAYWALL_RE.search(raw_text)):
                clean_text = truncate_content(raw_text, max_chars)
                cand_idx = seen_keys.get(canonical_key(c["url"]))
                score = score_map.get(cand_idx) if cand_idx is not None else None
                instant_sources[c["url"]] = SourceChunk(
                    url=c["url"],
                    title=c.get("title", ""),
                    content=clean_text,
                    fetch_tier="search_instant",
                    content_length=len(raw_text),
                    relevance_score=score,
                    fetch_time_ms=0.0,
                )
            else:
                urls_to_fetch.append(c)

        fetched_sources: list[SourceChunk] = []
        fetched_count = len(instant_sources)
        failed_count = 0

        if urls_to_fetch:
            prefetch_tasks: dict[str, asyncio.Task] = {}
            f_sources, f_fetched, f_failed, _ = await fetch_step(
                urls_to_fetch,
                seen_keys,
                score_map,
                prefetch_tasks,
                query,
                self._fetch,
                self._settings,
            )
            fetched_sources = f_sources
            fetched_count += f_fetched
            failed_count += f_failed

        fetched_map = {s.url: s for s in fetched_sources}
        sources: list[SourceChunk] = []
        for c in cands:
            u = c["url"]
            if u in instant_sources:
                sources.append(instant_sources[u])
            elif u in fetched_map:
                sources.append(fetched_map[u])

        return sources, fetched_count, failed_count

    def _select_diverse_candidates(
        self,
        deduped: list[dict[str, str]],
        reranked_indices: list[int],
        k: int,
    ) -> list[dict[str, str]]:
        """Select top-k candidates with domain diversity constraints."""
        top_urls: list[dict[str, str]] = []
        domain_counts: dict[str, int] = {}
        max_per_domain = self._settings.MAX_PER_DOMAIN_SOURCES

        for idx in reranked_indices:
            cand = deduped[idx]
            domain = urlparse(cand["url"]).netloc.lower()
            if domain_counts.get(domain, 0) < max_per_domain:
                top_urls.append(cand)
                domain_counts[domain] = domain_counts.get(domain, 0) + 1
                if len(top_urls) >= k:
                    break

        if len(top_urls) < min(k, len(reranked_indices)):
            existing = {u["url"] for u in top_urls}
            for idx in reranked_indices:
                cand = deduped[idx]
                if cand["url"] not in existing:
                    top_urls.append(cand)
                    if len(top_urls) >= k:
                        break
        return top_urls

    async def research(
        self,
        query: str,
        max_sub_queries: int = 3,
        fetch_top_k: int = 8,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        request: Request | None = None,
    ) -> RetrieveResponse:
        """Execute full 2-hop adaptive deep research pipeline."""
        start_time = time.perf_counter()
        log.info("Starting 2-Hop Adaptive Deep Research for '%s'", query)

        # ── Step 1: Decomposition & Planning ─────────────────────────────
        sub_queries = await self.decompose_query(query)
        sub_queries = sub_queries[:max_sub_queries]
        if query not in sub_queries:
            sub_queries.insert(0, query)

        await check_disconnect(request)

        # ── Hop 1: Parallel search across subqueries ─────────────────────
        hop1_tasks = [
            search_step(
                self._search,
                sq,
                8,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                hybrid=True,
            )
            for sq in sub_queries
        ]
        hop1_results_lists = await asyncio.gather(*hop1_tasks, return_exceptions=True)

        combined_hop1: list[dict[str, Any]] = []
        for res in hop1_results_lists:
            if isinstance(res, tuple) and res[0]:
                combined_hop1.extend(res[0])

        if not combined_hop1:
            log.warning("Deep Research found 0 search results across Hop 1 sub-queries")
            return RetrieveResponse(
                query=query,
                answer="No relevant information found across deep research sources.",
                report="No relevant information found across deep research sources.",
                citations=[],
                sources=[],
                sources_fetched=0,
                sources_failed=0,
            )

        deduped1, seen_keys = dedup_step(combined_hop1)
        if include_domains:
            inc_set = {d.strip().lower() for d in include_domains if d.strip()}
            deduped1 = [d for d in deduped1 if any(inc in d["url"].lower() for inc in inc_set)]
        if exclude_domains:
            exc_set = {d.strip().lower() for d in exclude_domains if d.strip()}
            deduped1 = [d for d in deduped1 if not any(exc in d["url"].lower() for exc in exc_set)]

        if not deduped1:
            return RetrieveResponse(
                query=query,
                answer="All deep research search results were excluded by domain filters.",
                report="All deep research search results were excluded by domain filters.",
                citations=[],
                sources=[],
                sources_fetched=0,
                sources_failed=0,
            )

        reranked_indices1, score_map1 = await rerank_step(
            query, deduped1, fetch_top_k, self._rerank, self._settings,
        )
        await check_disconnect(request)

        # Select Hop 1 candidate pool (half the budget)
        hop1_k = max(2, (fetch_top_k + 1) // 2)
        top_hop1 = self._select_diverse_candidates(deduped1, reranked_indices1, hop1_k)

        hop1_sources, hop1_fetched, hop1_failed = await self._fetch_candidates(
            top_hop1, query, seen_keys, score_map1,
        )
        await check_disconnect(request)

        # ── Step 3: Reflection & Gap Analysis ────────────────────────────
        gap_queries = await self.gap_analysis(query, hop1_sources)
        await check_disconnect(request)

        # ── Hop 2: Targeted searches for gap queries ─────────────────────
        hop2_sources: list[SourceChunk] = []
        hop2_fetched = 0
        hop2_failed = 0
        remaining_budget = max(1, fetch_top_k - len(hop1_sources))

        if gap_queries:
            log.info("Executing Hop 2 searches for gap queries: %s", gap_queries)
            hop2_tasks = [
                search_step(
                    self._search,
                    gq,
                    6,
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                    hybrid=True,
                )
                for gq in gap_queries
            ]
            hop2_results_lists = await asyncio.gather(*hop2_tasks, return_exceptions=True)
            combined_hop2: list[dict[str, Any]] = []
            for res in hop2_results_lists:
                if isinstance(res, tuple) and res[0]:
                    combined_hop2.extend(res[0])

            # Dedup Hop 2 against already seen Hop 1 URLs
            new_candidates: list[dict[str, str]] = []
            hop2_seen_keys: dict[str, int] = {}
            for item in combined_hop2:
                key = canonical_key(item["url"])
                if key not in seen_keys and key not in hop2_seen_keys:
                    hop2_seen_keys[key] = len(new_candidates)
                    new_candidates.append(item)

            if new_candidates:
                reranked_indices2, score_map2 = await rerank_step(
                    query, new_candidates, remaining_budget, self._rerank, self._settings,
                )
                top_hop2 = self._select_diverse_candidates(
                    new_candidates, reranked_indices2, remaining_budget,
                )
                hop2_sources, hop2_fetched, hop2_failed = await self._fetch_candidates(
                    top_hop2, query, hop2_seen_keys, score_map2,
                )
                for k in hop2_seen_keys:
                    seen_keys[k] = len(seen_keys)

        # Fallback: if Hop 2 yielded few sources and we have unused Hop 1 candidates, fill up
        all_sources = hop1_sources + hop2_sources
        if len(all_sources) < fetch_top_k and len(reranked_indices1) > len(top_hop1):
            unused_hop1 = [
                deduped1[idx]
                for idx in reranked_indices1
                if deduped1[idx]["url"] not in {s.url for s in all_sources}
            ][: fetch_top_k - len(all_sources)]
            if unused_hop1:
                more_sources, m_fetched, m_failed = await self._fetch_candidates(
                    unused_hop1, query, seen_keys, score_map1,
                )
                all_sources.extend(more_sources)
                hop1_fetched += m_fetched
                hop1_failed += m_failed

        if not all_sources:
            return RetrieveResponse(
                query=query,
                answer="Failed to extract readable content from deep research candidate sources.",
                report="Failed to extract readable content from deep research candidate sources.",
                citations=[],
                sources=[],
                sources_fetched=0,
                sources_failed=hop1_failed + hop2_failed,
            )

        budget_step(all_sources, self._settings)

        # ── Step 5: Final Comprehensive Synthesis ────────────────────────
        report_text, citations = await self._synthesize_report(query, all_sources)
        log.info(
            "Deep Research completed in %.2fs (%d sources, %d citations)",
            time.perf_counter() - start_time,
            len(all_sources),
            len(citations),
        )

        return RetrieveResponse(
            query=query,
            answer=report_text,
            report=report_text,
            citations=citations,
            sources=all_sources,
            sources_fetched=hop1_fetched + hop2_fetched,
            sources_failed=hop1_failed + hop2_failed,
        )

    async def research_stream(
        self,
        query: str,
        max_sub_queries: int = 3,
        fetch_top_k: int = 8,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        request: Request | None = None,
    ) -> AsyncIterator[str]:
        """Execute 2-hop deep research and yield SSE progress events and tokens."""
        # ── Step 1: Planning ─────────────────────────────────────────────
        yield f"event: progress\ndata: {json.dumps({'step': 'planning'})}\n\n"
        sub_queries = await self.decompose_query(query)
        sub_queries = sub_queries[:max_sub_queries]
        if query not in sub_queries:
            sub_queries.insert(0, query)

        await check_disconnect(request)

        # ── Step 2: Hop 1 ────────────────────────────────────────────────
        hop1_progress = json.dumps({"step": "hop_1", "sub_queries": sub_queries})
        yield f"event: progress\ndata: {hop1_progress}\n\n"
        hop1_tasks = [
            search_step(
                self._search,
                sq,
                8,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                hybrid=True,
            )
            for sq in sub_queries
        ]
        hop1_results_lists = await asyncio.gather(*hop1_tasks, return_exceptions=True)
        combined_hop1: list[dict[str, Any]] = []
        for res in hop1_results_lists:
            if isinstance(res, tuple) and res[0]:
                combined_hop1.extend(res[0])

        deduped1, seen_keys = dedup_step(combined_hop1) if combined_hop1 else ([], {})
        if include_domains and deduped1:
            inc_set = {d.strip().lower() for d in include_domains if d.strip()}
            deduped1 = [d for d in deduped1 if any(inc in d["url"].lower() for inc in inc_set)]
        if exclude_domains and deduped1:
            exc_set = {d.strip().lower() for d in exclude_domains if d.strip()}
            deduped1 = [d for d in deduped1 if not any(exc in d["url"].lower() for exc in exc_set)]

        hop1_sources: list[SourceChunk] = []
        if deduped1:
            reranked_indices1, score_map1 = await rerank_step(
                query, deduped1, fetch_top_k, self._rerank, self._settings,
            )
            hop1_k = max(2, (fetch_top_k + 1) // 2)
            top_hop1 = self._select_diverse_candidates(deduped1, reranked_indices1, hop1_k)
            hop1_sources, _, _ = await self._fetch_candidates(
                top_hop1, query, seen_keys, score_map1,
            )

        await check_disconnect(request)

        # ── Step 3: Reflection & Gap Analysis ────────────────────────────
        yield f"event: progress\ndata: {json.dumps({'step': 'gap_analysis'})}\n\n"
        gap_queries = await self.gap_analysis(query, hop1_sources)
        await check_disconnect(request)

        # ── Step 4: Hop 2 ────────────────────────────────────────────────
        hop2_progress = json.dumps({"step": "hop_2", "gap_queries": gap_queries})
        yield f"event: progress\ndata: {hop2_progress}\n\n"
        hop2_sources: list[SourceChunk] = []
        remaining_budget = max(1, fetch_top_k - len(hop1_sources))

        if gap_queries:
            hop2_tasks = [
                search_step(
                    self._search,
                    gq,
                    6,
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                    hybrid=True,
                )
                for gq in gap_queries
            ]
            hop2_results_lists = await asyncio.gather(*hop2_tasks, return_exceptions=True)
            combined_hop2: list[dict[str, Any]] = []
            for res in hop2_results_lists:
                if isinstance(res, tuple) and res[0]:
                    combined_hop2.extend(res[0])

            new_candidates: list[dict[str, str]] = []
            hop2_seen_keys: dict[str, int] = {}
            for item in combined_hop2:
                key = canonical_key(item["url"])
                if key not in seen_keys and key not in hop2_seen_keys:
                    hop2_seen_keys[key] = len(new_candidates)
                    new_candidates.append(item)

            if new_candidates:
                reranked_indices2, score_map2 = await rerank_step(
                    query, new_candidates, remaining_budget, self._rerank, self._settings,
                )
                top_hop2 = self._select_diverse_candidates(
                    new_candidates, reranked_indices2, remaining_budget,
                )
                hop2_sources, _, _ = await self._fetch_candidates(
                    top_hop2, query, hop2_seen_keys, score_map2,
                )
                for k in hop2_seen_keys:
                    seen_keys[k] = len(seen_keys)

        all_sources = hop1_sources + hop2_sources
        if len(all_sources) < fetch_top_k and deduped1 and len(reranked_indices1) > len(top_hop1):
            unused_hop1 = [
                deduped1[idx]
                for idx in reranked_indices1
                if deduped1[idx]["url"] not in {s.url for s in all_sources}
            ][: fetch_top_k - len(all_sources)]
            if unused_hop1:
                more_sources, _, _ = await self._fetch_candidates(
                    unused_hop1, query, seen_keys, score_map1,
                )
                all_sources.extend(more_sources)

        if not all_sources:
            yield f"event: token\ndata: {json.dumps('No research sources found.')}\n\n"
            yield f"event: done\ndata: {json.dumps({'finish_reason': 'no_sources'})}\n\n"
            return

        budget_step(all_sources, self._settings)

        # Emit source events for UI integration
        for i, src in enumerate(all_sources, start=1):
            source_event = {
                "id": i,
                "url": src.url,
                "title": src.title,
                "relevance_score": src.relevance_score,
                "fetch_tier": src.fetch_tier,
            }
            yield f"event: source\ndata: {json.dumps(source_event)}\n\n"

        # ── Step 5: Final Synthesis ──────────────────────────────────────
        synth_progress = json.dumps({"step": "synthesizing", "sources": len(all_sources)})
        yield f"event: progress\ndata: {synth_progress}\n\n"
        async for token in self._stream_deep_synthesis(query, all_sources):
            yield f"event: token\ndata: {json.dumps(token)}\n\n"

        yield f"event: done\ndata: {json.dumps({'finish_reason': 'stop'})}\n\n"

    async def _synthesize_report(
        self, query: str, sources: list[SourceChunk]
    ) -> tuple[str, list[Citation]]:
        """Call LLM with deep research prompt and filter citations."""
        if not self._settings.LLM_CHAT_URL:
            raw_answer = _fallback_answer(sources)
            return verify_citations_step(raw_answer, sources)

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
            headers = (
                {"Authorization": f"Bearer {self._settings.LLM_API_KEY}"}
                if self._settings.LLM_API_KEY
                else {}
            )
            resp = await self._http.post(
                self._settings.LLM_CHAT_URL,
                json=payload,
                headers=headers,
                timeout=self._settings.SYNTHESIS_TIMEOUT,
            )
            if resp.status_code == 200:
                raw_answer = (
                    resp.json()
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
                return verify_citations_step(raw_answer, sources)
        except Exception as exc:
            log.warning("Deep synthesis call failed: %s", exc)

        raw_answer = _fallback_answer(sources)
        return verify_citations_step(raw_answer, sources)

    async def _stream_deep_synthesis(
        self, query: str, sources: list[SourceChunk]
    ) -> AsyncIterator[str]:
        """Stream tokens for deep report synthesis."""
        if not self._settings.LLM_CHAT_URL:
            yield _fallback_answer(sources)
            return

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
            "stream": True,
        }

        try:
            headers = (
                {"Authorization": f"Bearer {self._settings.LLM_API_KEY}"}
                if self._settings.LLM_API_KEY
                else {}
            )
            async with self._http.stream(
                "POST",
                self._settings.LLM_CHAT_URL,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(self._settings.SYNTHESIS_TIMEOUT, connect=10.0),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices", [])
                    if choices:
                        token = choices[0].get("delta", {}).get("content", "")
                        if token:
                            yield token
        except Exception as exc:
            log.warning("Streaming deep synthesis failed: %s", exc)
            yield _fallback_answer(sources)

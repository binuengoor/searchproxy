"""Retrieve pipeline steps — extracted from retrieve_service.py for modularity.

Each step is either a pure function or an async function that takes its
dependencies explicitly. This keeps the orchestrator (RetrieveService) thin
and makes individual steps testable without instantiating the full service.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from fastapi import Request

from app.config import Settings
from app.schemas import SourceChunk
from app.services.fetch_chain import FetchChain, _is_anti_bot_block
from app.services.models import FetchResult
from app.services.litellm_search import LiteLLMSearchClient
from app.services.rerank_service import RerankService

log = logging.getLogger(__name__)

# Paywall / login-wall indicators
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


def is_likely_paywall(content: str) -> bool:
    if len(content) < 200:
        return True
    return bool(_PAYWALL_RE.search(content))


def is_too_short(content: str, min_length: int) -> bool:
    return len(content) < min_length


def canonical_key(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{path}"


def truncate_content(content: str, max_chars: int) -> str:
    if len(content) <= max_chars:
        return content
    search_range = content[:max_chars]
    last_para = search_range.rfind("\n\n")
    if last_para > max_chars * 0.5:
        return content[:last_para].strip()
    last_sentence = max(search_range.rfind(".\n"), search_range.rfind(". "))
    if last_sentence > max_chars * 0.5:
        return content[:last_sentence + 1].strip()
    return content[:max_chars].strip()


async def check_disconnect(request: Request | None) -> None:
    from fastapi import HTTPException
    if request is not None and await request.is_disconnected():
        raise HTTPException(status_code=499, detail="Client closed request")


async def search_step(
    search_client: LiteLLMSearchClient,
    query: str,
    max_results: int,
) -> tuple[list[dict[str, str]], int]:
    log.info("Retrieve pipeline: search for '%s' (max_results=%d)", query, max_results)
    search_resp = await search_client.search(query=query, max_results=max_results)
    if not search_resp.results:
        log.warning("Retrieve pipeline: no search results for '%s'", query)
        return [], 0
    results = [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in search_resp.results]
    return results, len(results)


def dedup_step(
    results: list[dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, int]]:
    seen_keys: dict[str, int] = {}
    deduped: list[dict[str, str]] = []
    for r in results:
        key = canonical_key(r["url"])
        if key not in seen_keys:
            seen_keys[key] = len(deduped)
            deduped.append(r)
    log.info("Retrieve pipeline: %d results after dedup (from %d)", len(deduped), len(results))
    return deduped, seen_keys


async def rerank_step(
    query: str,
    deduped: list[dict[str, str]],
    fetch_top_k: int,
    rerank_service: RerankService,
    settings: Settings,
) -> tuple[list[int], dict[int, float]]:
    # Fast path: if we are fetching all deduped results anyway, skip the
    # reranker entirely. Saves 1-3s of pure overhead.
    if len(deduped) <= fetch_top_k:
        log.info(
            "Retrieve pipeline: skipping rerank (%d results <= fetch_top_k=%d)",
            len(deduped), fetch_top_k,
        )
        return list(range(len(deduped))), {}

    rerank_docs = [
        f"{d['title']}: {d['snippet']}" if d["title"] else d["snippet"]
        for d in deduped
    ]
    top_k_rerank = min(settings.RETRIEVE_RERANK_TOP_K, len(rerank_docs))
    rerank_results = await rerank_service.rerank(query=query, documents=rerank_docs, top_k=top_k_rerank)
    if rerank_results is not None:
        reranked_indices = [r.index for r in rerank_results]
        score_map = {r.index: r.relevance_score for r in rerank_results}
        log.info("Retrieve pipeline: reranker returned %d results", len(rerank_results))
        return reranked_indices, score_map
    log.info("Retrieve pipeline: reranker unavailable, using original order")
    return list(range(len(deduped))), {}


def _process_fetch_result(
    url_info: dict[str, str],
    result: Any,
    prefetch_tasks: dict[str, asyncio.Task],
    seen_keys: dict[str, int],
    score_map: dict[int, float],
    settings: Settings,
) -> tuple[SourceChunk | None, int, int]:
    """Apply quality gates to a single fetch result. Returns (source, failed, skipped)."""
    min_content_length = settings.RETRIEVE_MIN_CONTENT_LENGTH

    if isinstance(result, Exception):
        log.warning("Fetch exception for %s: %s", url_info["url"], result)
        return None, 1, 0
    if not result.success:
        log.warning("Fetch failed for %s: %s", url_info["url"], result.error)
        return None, 1, 0

    content = result.markdown

    if url_info["url"] in prefetch_tasks and _is_anti_bot_block(result.status_code, content):
        # Signal caller that a re-fetch is needed
        return None, 0, 0

    if is_too_short(content, min_content_length):
        log.info("Skipping source %s: content too short (%d chars < %d min)", url_info["url"], len(content), min_content_length)
        return None, 0, 1
    if is_likely_paywall(content):
        log.info("Skipping source %s: detected paywall/login wall", url_info["url"])
        return None, 0, 1

    original_idx = seen_keys.get(canonical_key(url_info["url"]))
    relevance_score = score_map.get(original_idx) if original_idx is not None else None
    content = truncate_content(content, settings.RETRIEVE_MAX_CONTENT_PER_SOURCE)

    source = SourceChunk(
        url=url_info["url"],
        title=result.title or url_info["title"],
        content=content,
        fetch_tier=result.source or None,
        content_length=len(content),
        relevance_score=relevance_score,
        fetch_time_ms=result.fetch_time_ms,
    )
    return source, 0, 0


async def _refetch_anti_bot(
    url: str,
    fetch_chain: FetchChain,
    per_url_timeout: float,
    content_filter: str | None,
    content_query: str | None,
) -> FetchResult | None:
    """Re-fetch a URL with the full chain (including anti-bot) after a prefetch anti-bot block."""
    log.info("Re-fetching %s with full chain (prefetch hit anti-bot block)", url)
    try:
        async with asyncio.timeout(per_url_timeout):
            result = await fetch_chain.execute(
                url,
                aggressive_clean=True,
                skip_firebreak=False,
                content_filter=content_filter,
                content_query=content_query,
            )
        if not result.success:
            log.warning("Re-fetch failed for %s: %s", url, result.error)
            return None
        return result
    except asyncio.TimeoutError:
        log.warning("Re-fetch timed out for %s", url)
        return None


async def fetch_step(
    top_urls: list[dict[str, str]],
    seen_keys: dict[str, int],
    score_map: dict[int, float],
    prefetch_tasks: dict[str, asyncio.Task],
    query: str,
    fetch_chain: FetchChain,
    settings: Settings,
) -> tuple[list[SourceChunk], int, int, int]:
    top_url_set = {u["url"] for u in top_urls}
    fetch_tasks: list[asyncio.Task] = []
    batch_timeout = settings.RETRIEVE_FETCH_TIMEOUT
    per_url_timeout = max(4.0, min(10.0, batch_timeout / max(len(top_urls), 1) + 2))
    content_filter = "bm25"
    content_query = query

    for url_info in top_urls:
        url = url_info["url"]
        if url in prefetch_tasks:
            fetch_tasks.append(prefetch_tasks[url])
        else:
            async def _fetch_one(
                u: str = url,
                cf: str | None = content_filter,
                cq: str | None = content_query,
            ) -> FetchResult:
                async with asyncio.timeout(per_url_timeout):
                    return await fetch_chain.execute(
                        u,
                        aggressive_clean=True,
                        skip_firebreak=False,
                        content_filter=cf,
                        content_query=cq,
                    )
            fetch_tasks.append(asyncio.create_task(_fetch_one(), name=f"fetch:{url[:80]}"))

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

    for url, task in prefetch_tasks.items():
        if url not in top_url_set:
            task.cancel()

    sources: list[SourceChunk] = []
    sources_failed = 0
    sources_skipped_quality = 0

    for url_info, result in zip(top_urls, fetch_results):
        src, failed, skipped = _process_fetch_result(
            url_info, result, prefetch_tasks, seen_keys, score_map, settings,
        )
        # Handle anti-bot re-fetch for prefetch hits
        if src is None and failed == 0 and skipped == 0 and url_info["url"] in prefetch_tasks:
            refetch = await _refetch_anti_bot(
                url_info["url"], fetch_chain, per_url_timeout, content_filter, content_query,
            )
            if refetch is not None:
                src, failed, skipped = _process_fetch_result(
                    url_info, refetch, {}, seen_keys, score_map, settings,
                )
            else:
                failed = 1
        sources_failed += failed
        sources_skipped_quality += skipped
        if src is not None:
            sources.append(src)

    sources_fetched = len(sources)
    log.info(
        "Retrieve pipeline: fetched %d/%d sources (%d failed, %d skipped by quality gates)",
        sources_fetched, len(top_urls), sources_failed, sources_skipped_quality,
    )
    return sources, sources_fetched, sources_failed, sources_skipped_quality


async def fetch_step_incremental(
    top_urls: list[dict[str, str]],
    seen_keys: dict[str, int],
    score_map: dict[int, float],
    prefetch_tasks: dict[str, asyncio.Task],
    query: str,
    fetch_chain: FetchChain,
    settings: Settings,
) -> AsyncIterator[tuple[SourceChunk | None, int, int]]:
    """Fetch URLs and yield results incrementally as each one completes.

    Yields (source, failed_increment, skipped_increment) for every completed
    fetch so the caller can stream source events to the client immediately.
    Anti-bot re-fetches are handled inline before yielding.
    """
    top_url_set = {u["url"] for u in top_urls}
    batch_timeout = settings.RETRIEVE_FETCH_TIMEOUT
    per_url_timeout = max(4.0, min(10.0, batch_timeout / max(len(top_urls), 1) + 2))
    content_filter = "bm25"
    content_query = query

    # Track pending tasks with their URL so we never have to guess which
    # result belongs to which URL after completion.
    pending: dict[asyncio.Task, str] = {}

    for url_info in top_urls:
        url = url_info["url"]
        if url in prefetch_tasks:
            pending[prefetch_tasks[url]] = url
        else:
            async def _fetch_one(
                u: str = url,
                cf: str | None = content_filter,
                cq: str | None = content_query,
            ) -> FetchResult:
                async with asyncio.timeout(per_url_timeout):
                    return await fetch_chain.execute(
                        u,
                        aggressive_clean=True,
                        skip_firebreak=False,
                        content_filter=cf,
                        content_query=cq,
                    )
            task = asyncio.create_task(_fetch_one(), name=f"fetch:{url[:80]}")
            pending[task] = url

    # Cancel unused prefetches
    for url, task in prefetch_tasks.items():
        if url not in top_url_set:
            task.cancel()

    url_map = {u["url"]: u for u in top_urls}

    try:
        while pending:
            done, _ = await asyncio.wait(
                pending.keys(), return_when=asyncio.FIRST_COMPLETED, timeout=batch_timeout,
            )
            if not done:
                # Timeout on the whole batch: cancel everything remaining
                for task in list(pending.keys()):
                    task.cancel()
                    yield None, 1, 0
                break

            for task in done:
                url = pending.pop(task)
                try:
                    result = task.result()
                except asyncio.TimeoutError:
                    result = asyncio.TimeoutError(f"Fetch timed out after {per_url_timeout:.1f}s")
                except Exception as exc:
                    result = exc

                url_info = url_map[url]
                src, failed, skipped = _process_fetch_result(
                    url_info, result, prefetch_tasks, seen_keys, score_map, settings,
                )
                # Anti-bot re-fetch for prefetch hits
                if src is None and failed == 0 and skipped == 0 and url in prefetch_tasks:
                    refetch = await _refetch_anti_bot(
                        url, fetch_chain, per_url_timeout, content_filter, content_query,
                    )
                    if refetch is not None:
                        src, failed, skipped = _process_fetch_result(
                            url_info, refetch, {}, seen_keys, score_map, settings,
                        )
                    else:
                        failed = 1
                yield src, failed, skipped
    except asyncio.TimeoutError:
        for task in list(pending.keys()):
            task.cancel()
            yield None, 1, 0


def budget_step(sources: list[SourceChunk], settings: Settings) -> list[SourceChunk]:
    total_content = sum(len(s.content) for s in sources)
    if total_content <= settings.RETRIEVE_MAX_TOTAL_CONTENT:
        return sources

    budget = settings.RETRIEVE_MAX_TOTAL_CONTENT
    scores = [s.relevance_score or 0.5 for s in sources]
    total_weight = sum(scores)
    if total_weight == 0:
        total_weight = len(sources) * 0.5
    per_source_budgets = [
        max(budget // len(sources) // 2, int(budget * (w / total_weight)))
        for w in scores
    ]
    total_budget = sum(per_source_budgets)
    if total_budget > budget:
        scale = budget / total_budget
        per_source_budgets = [int(b * scale) for b in per_source_budgets]

    for i in range(len(sources)):
        sources[i].content = truncate_content(sources[i].content, per_source_budgets[i])

    actual_total = sum(len(s.content) for s in sources)
    log.info(
        "Total content truncated from %d to %d chars (relevance-weighted budget)",
        total_content, actual_total,
    )
    return sources

"""BGE reranker client via cf-inference.

Calls the /v1/rerank endpoint on cf-inference (Cloudflare Workers AI) to
rerank search results by relevance to the query. Falls back gracefully —
if reranking fails, original order is preserved.

Supports optional caching: if CacheService is provided, rerank results are
cached with a configurable TTL. Same query + same documents = same scores,
so caching is safe and deterministic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel

from app.config import Settings
from app.services.local_reranker import LocalReranker

if TYPE_CHECKING:
    from app.services.cache import CacheService

log = logging.getLogger(__name__)


class RerankResult(BaseModel):
    """Normalized reranker result."""

    index: int
    relevance_score: float
    text: str


class RerankService:
    """Reranker orchestrating local ONNX cross-encoder with cf-inference remote fallback.

    If both local and remote fail, the service returns None so the caller can fall
    back to original ordering.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        cache: CacheService | None = None,
        local_reranker: LocalReranker | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._cache = cache
        self._local_reranker = local_reranker if local_reranker is not None else LocalReranker(settings=settings)

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
    ) -> list[RerankResult] | None:
        """Rerank documents by relevance to the query.

        Args:
            query: The search query.
            documents: List of document strings (title + snippet) to rank.
            top_k: Return only the top K results. Defaults to all.

        Returns:
            Ordered list of RerankResult, or None if the reranker fails.
        """
        if not documents:
            return []

        # ── Cache read ────────────────────────────────────────────────
        if self._cache is not None:
            cached = await self._cache.get_rerank(query, documents)
            if cached is not None:
                log.info("Cache HIT for rerank: '%s' (%d documents)", query, len(documents))
                try:
                    return [RerankResult(index=r["index"], relevance_score=r["relevance_score"], text=r["text"]) for r in cached]
                except Exception as exc:
                    log.warning("Rerank cache deserialization failed for '%s': %s", query, exc)

        # ── Primary: Local ONNX Cross-Encoder ─────────────────────────
        if self._settings.RERANK_LOCAL:
            try:
                log.info("Reranking %d documents locally with ONNX for query '%s'", len(documents), query)
                scores = await self._local_reranker.rerank(query, documents)
                indexed_scores = [(i, score, doc) for i, (score, doc) in enumerate(zip(scores, documents))]
                indexed_scores.sort(key=lambda x: x[1], reverse=True)
                if top_k is not None:
                    indexed_scores = indexed_scores[:top_k]

                results = [
                    RerankResult(index=i, relevance_score=score, text=doc)
                    for i, score, doc in indexed_scores
                ]
                log.info("Local ONNX reranker returned %d results for query '%s'", len(results), query)

                if self._cache is not None and results:
                    cache_data = [{"index": r.index, "relevance_score": r.relevance_score, "text": r.text} for r in results]
                    await self._cache.set_rerank(query, documents, cache_data)

                return results
            except Exception as exc:
                log.warning("Local ONNX reranker failed for query '%s': %s; falling back to remote", query, exc)

        # ── Secondary: Remote cf-inference fallback ──────────────────
        url = self._settings.CF_RERANK_URL
        if not url:
            log.warning("No remote CF_RERANK_URL configured; falling back to original search order")
            return None
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._settings.CF_RERANK_API_KEY:
            headers["Authorization"] = f"Bearer {self._settings.CF_RERANK_API_KEY}"

        body: dict[str, object] = {
            "query": query,
            "documents": documents,
            "model": self._settings.CF_RERANK_MODEL,
        }
        if top_k is not None:
            body["top_k"] = top_k

        log.info("Reranking %d documents for query '%s' (top_k=%s)", len(documents), query, top_k)

        try:
            response = await self._client.post(
                url,
                json=body,
                headers=headers,
                timeout=httpx.Timeout(self._settings.RERANK_TIMEOUT, connect=self._settings.CONNECT_TIMEOUT),
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            log.warning("Reranker timed out for query '%s'", query)
            return None
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Reranker returned HTTP %d for query '%s'",
                exc.response.status_code,
                query,
            )
            return None
        except Exception as exc:
            log.warning("Reranker failed for query '%s': %s", query, exc)
            return None

        # Parse cf-inference response shape:
        # {"results": [{"index": 0, "relevance_score": 0.95, "document": {"text": "..."}}], "model": "..."}
        raw_results = data.get("results", [])
        if not raw_results:
            log.warning("Reranker returned empty results for query '%s'", query)
            return None

        remote_results: list[RerankResult] = []
        for item in raw_results:
            idx = item.get("index", 0)
            score = item.get("relevance_score", 0.0)
            text = item.get("document", {}).get("text", "")
            remote_results.append(RerankResult(index=idx, relevance_score=score, text=text))

        log.info("Reranker returned %d results for query '%s'", len(remote_results), query)

        # ── Cache write ───────────────────────────────────────────────
        if self._cache is not None and remote_results:
            cache_data = [{"index": r.index, "relevance_score": r.relevance_score, "text": r.text} for r in remote_results]
            await self._cache.set_rerank(query, documents, cache_data)

        return remote_results

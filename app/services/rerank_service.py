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
from dataclasses import dataclass

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


@dataclass
class RerankResult:
    """A single reranked result with its relevance score."""

    index: int
    relevance_score: float
    text: str


class RerankService:
    """Async client for the BGE reranker on cf-inference.

    If the rerank call fails (timeout, HTTP error, parse failure), the
    service returns None so the caller can fall back to original ordering.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        cache: "CacheService | None" = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._cache = cache

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

        url = self._settings.CF_RERANK_URL
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
                timeout=httpx.Timeout(15.0, connect=self._settings.CONNECT_TIMEOUT),
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

        results: list[RerankResult] = []
        for item in raw_results:
            idx = item.get("index", 0)
            score = item.get("relevance_score", 0.0)
            text = item.get("document", {}).get("text", "")
            results.append(RerankResult(index=idx, relevance_score=score, text=text))

        log.info("Reranker returned %d results for query '%s'", len(results), query)

        # ── Cache write ───────────────────────────────────────────────
        if self._cache is not None and results:
            cache_data = [{"index": r.index, "relevance_score": r.relevance_score, "text": r.text} for r in results]
            await self._cache.set_rerank(query, documents, cache_data)

        return results

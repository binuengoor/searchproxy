from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, Field

from app.config import Settings

log = logging.getLogger(__name__)


class SearchResult(BaseModel):
    """A single search result from LiteLLM."""

    title: str
    url: str
    snippet: str


class SearchResponse(BaseModel):
    """Search results wrapper matching Perplexity/OpenAI /v1/search shape."""

    results: list[SearchResult] = Field(default_factory=list)


class LiteLLMSearchClient:
    """Standalone async client for the LiteLLM search router.

    Does not reach into other services. Owns its own request logic.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def search(
        self,
        query: str,
        max_results: int = 10,
    ) -> SearchResponse:
        """POST to LiteLLM router, return normalized results.

        Graceful degradation: on any error (timeout, HTTP error, parse failure)
        returns ``SearchResponse(results=[])`` so callers always get a valid
        response and never need to handle exceptions.
        """
        log.info("Searching LiteLLM for '%s'", query)

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._settings.LITELLM_API_KEY:
            headers["Authorization"] = f"Bearer {self._settings.LITELLM_API_KEY}"
        elif self._settings.LITELLM_SEARCH_URL.startswith("https://"):
            log.warning(
                "LITELLM_SEARCH_URL uses https but no LITELLM_API_KEY is set"
            )

        body = {"query": query, "max_results": max_results}

        timeout = httpx.Timeout(
            timeout=float(self._settings.SEARCH_TIMEOUT),
            connect=5.0,
        )

        try:
            response = await self._client.post(
                self._settings.LITELLM_SEARCH_URL,
                json=body,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            log.warning("LiteLLM search timed out for query '%s'", query)
            return SearchResponse(results=[])
        except httpx.HTTPStatusError as exc:
            log.warning(
                "LiteLLM search returned HTTP %d for query '%s'",
                exc.response.status_code,
                query,
            )
            return SearchResponse(results=[])
        except Exception as exc:
            log.warning("LiteLLM search failed for query '%s': %s", query, exc)
            return SearchResponse(results=[])

        # Normalize LiteLLM response: may be {"results": [...]} or the
        # OpenAI-compatible {"object": "search", "results": [...]} shape.
        raw_results: list[dict[str, str]] = []
        if isinstance(data, dict):
            raw_results = data.get("results", [])

        results = [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("snippet") or r.get("content", ""),
            )
            for r in raw_results
            if isinstance(r, dict)
        ]

        log.info(
            "LiteLLM returned %d results for '%s'",
            len(results),
            query,
        )
        return SearchResponse(results=results)

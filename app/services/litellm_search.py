from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings

log = logging.getLogger(__name__)

# Fire the HTTPS-without-key warning at most once per process lifetime.
_warned_https_no_key = False


class SearchResult(BaseModel):
    """A single search result from LiteLLM."""

    title: str = Field(..., description="Page title.")
    url: str = Field(..., description="Source URL.")
    snippet: str = Field(..., description="Short content summary (excerpt) from the page.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "title": "Real Madrid CF - Wikipedia",
                    "url": "https://en.wikipedia.org/wiki/Real_Madrid_CF",
                    "snippet": "Real Madrid Club de Fútbol, commonly referred to as Real Madrid, is a Spanish professional football club based in Madrid...",
                }
            ]
        }
    )


class SearchResponse(BaseModel):
    """Search results wrapper matching Perplexity search shape."""

    results: list[SearchResult] = Field(
        default_factory=list,
        description="List of search results; may be empty on error or no matches.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "results": [
                        {
                            "title": "Real Madrid CF - Wikipedia",
                            "url": "https://en.wikipedia.org/wiki/Real_Madrid_CF",
                            "snippet": "Real Madrid Club de Fútbol, commonly referred to as Real Madrid...",
                        }
                    ]
                }
            ]
        }
    )

class LiteLLMSearchClient:
    """Standalone async client for the LiteLLM search router.

    Does not reach into other services. Owns its own request logic.
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
        self._timeout = httpx.Timeout(
            timeout=float(settings.SEARCH_TIMEOUT),
            connect=self._settings.CONNECT_TIMEOUT,
        )

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

        # --- Cache read ---
        if self._cache is not None:
            cached = await self._cache.get_search(query, max_results)
            if cached is not None:
                log.info("Cache HIT for search: '%s'", query)
                try:
                    return SearchResponse.model_validate(cached)
                except Exception as exc:
                    log.warning("Cache deserialization failed for '%s': %s", query, exc)
            else:
                log.info("Cache MISS for search: '%s'", query)

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._settings.LITELLM_API_KEY:
            headers["Authorization"] = f"Bearer {self._settings.LITELLM_API_KEY}"
        elif self._settings.LITELLM_SEARCH_URL.startswith("https://"):
            global _warned_https_no_key
            if not _warned_https_no_key:
                log.warning(
                    "LITELLM_SEARCH_URL uses https but no LITELLM_API_KEY is set"
                )
                _warned_https_no_key = True

        body = {"query": query, "max_results": max_results}

        try:
            response = await self._client.post(
                self._settings.LITELLM_SEARCH_URL,
                json=body,
                headers=headers,
                timeout=self._timeout,
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

        search_resp = SearchResponse(results=results)

        log.info(
            "LiteLLM returned %d results for '%s'",
            len(results),
            query,
        )

        # --- Cache write ---
        if self._cache is not None and results:
            await self._cache.set_search(query, max_results, search_resp.model_dump())

        return search_resp

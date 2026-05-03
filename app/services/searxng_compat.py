from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.services.litellm_search import LiteLLMSearchClient, SearchResponse

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SearXNG response models
# ---------------------------------------------------------------------------


class SearxngResult(BaseModel):
    """A single result in the SearXNG JSON format."""

    title: str
    url: str
    content: str = ""
    engine: str = "unknown"
    score: float = 0.0
    category: str = "general"


class SearxngResponse(BaseModel):
    """SearXNG-compatible JSON response.

    Includes all standard SearXNG fields. Extra fields that LiteLLM cannot
    provide are returned as empty arrays.
    """

    query: str = ""
    number_of_results: int = 0
    results: list[SearxngResult] = Field(default_factory=list)
    answers: list[str] = Field(default_factory=list)
    corrections: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    infoboxes: list[dict] = Field(default_factory=list)
    unresponsive_engines: list[str] = Field(default_factory=list)


class SearxngParams(BaseModel):
    """Parsed and validated SearXNG query parameters."""

    q: str
    categories: str | None = None
    engines: str | None = None
    language: str | None = None
    pageno: int | None = None
    time_range: str | None = None
    safesearch: int | None = None
    autocomplete: str | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

# Categories that require a passthrough to upstream SearXNG.
_MEDIA_CATEGORIES = frozenset(("images", "videos"))


class SearxngCompatService:
    """Bridge between SearXNG JSON API and the LiteLLM search relay.

    Thin orchestration: decides whether to passthrough to an upstream SearXNG
    instance (media queries) or to normalize a LiteLLM response into the
    SearXNG format.
    """

    def __init__(
        self,
        litellm_client: LiteLLMSearchClient,
        http_client: httpx.AsyncClient,
        settings: Settings,
    ) -> None:
        self._litellm = litellm_client
        self._http = http_client
        self._settings = settings

    def _should_passthrough(self, params: SearxngParams) -> bool:
        """Return True when the request should be forwarded to upstream SearXNG."""
        if params.categories and params.categories in _MEDIA_CATEGORIES:
            return True
        # Also passthrough for explicit media engines if upstream is configured.
        if params.engines:
            media_engines = {"bing images", "bing videos", "youtube", "google images"}
            requested = {e.strip().lower() for e in params.engines.split(",")}
            if requested & media_engines:
                return True
        return False

    async def search(self, params: SearxngParams) -> SearxngResponse:
        """Execute the appropriate search strategy and return a SearXNG-shaped response."""
        if self._should_passthrough(params):
            return await self._passthrough(params)

        return await self._litellm_normalize(params)

    async def _passthrough(self, params: SearxngParams) -> SearxngResponse:
        """Forward the request directly to the upstream SearXNG instance."""
        if not self._settings.SEARXNG_URL:
            log.info(
                "SearXNG passthrough requested but SEARXNG_URL is not configured; "
                "returning empty results"
            )
            return SearxngResponse(query=params.q)

        log.info(
            "Passthrough SearXNG request to upstream for q='%s' categories=%s",
            params.q,
            params.categories,
        )

        query_params = {"q": params.q, "format": "json"}
        if params.categories:
            query_params["categories"] = params.categories
        if params.engines:
            query_params["engines"] = params.engines
        if params.pageno is not None:
            query_params["pageno"] = params.pageno
        if params.time_range:
            query_params["time_range"] = params.time_range
        if params.safesearch is not None:
            query_params["safesearch"] = params.safesearch
        if params.autocomplete:
            query_params["autocomplete"] = params.autocomplete

        timeout = httpx.Timeout(
            timeout=float(self._settings.SEARCH_TIMEOUT),
            connect=5.0,
        )

        try:
            response = await self._http.get(
                self._settings.SEARXNG_URL,
                params=query_params,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            log.warning("Upstream SearXNG timed out for q='%s'", params.q)
            return SearxngResponse(
                query=params.q,
                unresponsive_engines=["searxng"],
            )
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Upstream SearXNG returned HTTP %d for q='%s'",
                exc.response.status_code,
                params.q,
            )
            return SearxngResponse(
                query=params.q,
                unresponsive_engines=["searxng"],
            )
        except Exception as exc:
            log.warning("Upstream SearXNG request failed for q='%s': %s", params.q, exc)
            return SearxngResponse(
                query=params.q,
                unresponsive_engines=["searxng"],
            )

        # Forward the raw SearXNG JSON as-is, renaming key fields to match our model.
        raw_results = data.get("results", [])
        results = [
            SearxngResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                content=r.get("content", ""),
                engine=r.get("engine", "searxng"),
                score=r.get("score", 0.0),
                category=r.get("category", "general"),
            )
            for r in raw_results
            if isinstance(r, dict)
        ]

        return SearxngResponse(
            query=data.get("query", params.q),
            number_of_results=data.get("number_of_results", len(results)),
            results=results,
            answers=data.get("answers", []),
            corrections=data.get("corrections", []),
            suggestions=data.get("suggestions", []),
            infoboxes=data.get("infoboxes", []),
            unresponsive_engines=data.get("unresponsive_engines", []),
        )

    async def _litellm_normalize(self, params: SearxngParams) -> SearxngResponse:
        """Call LiteLLM search and normalize the response to SearXNG format."""
        litellm_resp: SearchResponse = await self._litellm.search(
            query=params.q,
            max_results=10,
        )

        results = [
            SearxngResult(
                title=r.title,
                url=r.url,
                content=r.snippet,
                engine="unknown",
                score=0.0,
                category="general",
            )
            for r in litellm_resp.results
        ]

        log.info(
            "Normalized %d LiteLLM results to SearXNG format for q='%s'",
            len(results),
            params.q,
        )

        return SearxngResponse(
            query=params.q,
            number_of_results=len(results),
            results=results,
            answers=[],
            corrections=[],
            suggestions=[],
            infoboxes=[],
            unresponsive_engines=[],
        )

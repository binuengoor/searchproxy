from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.dependencies import get_litellm_client
from app.schemas import MessageItem
from app.services.litellm_search import LiteLLMSearchClient, SearchResponse

log = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["search"])


class PerplexityQuery(BaseModel):
    """Request body for /compat/perplexity and /v1/search.

    Supports two shapes:

    - Simple (preferred): ``{"query": "..."}``
    - Open WebUI / Perplexity-compatible: ``{"messages": [{"role": "user", "content": "..."}]}``

    When ``messages`` is provided, the query is extracted from the **last**
    ``user`` message. All other Perplexity fields (``model``, ``stream``,
    ``return_related_questions``, ``search_recency_filter``) are accepted but
    ignored.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"query": "Real Madrid 2025 season"},
                {
                    "messages": [
                        {"role": "user", "content": "What is the capital of Canada?"}
                    ]
                },
            ]
        }
    )

    query: str = Field(
        default="",
        description="Search query string. Mutually exclusive with ``messages`` — provide one or the other.",
    )
    max_results: int = Field(
        default=10, ge=1, le=100, description="Maximum results to return"
    )
    # —— Open WebUI / Perplexity compat fields (ignored) ——
    messages: list[MessageItem] = Field(
        default=[],
        description="OpenAI-style messages array. If provided, query is extracted from the last user message.",
    )
    model: str = Field(default="", description="Ignored — forwarded for Open WebUI compat.")
    stream: bool = Field(default=False, description="Ignored — forwarded for Open WebUI compat.")
    return_related_questions: bool = Field(
        default=False, description="Ignored — forwarded for Open WebUI compat."
    )
    search_recency_filter: str = Field(
        default="", description="Ignored — forwarded for Open WebUI compat."
    )

    @model_validator(mode="after")
    def _extract_query(self) -> "PerplexityQuery":
        if not self.query and self.messages:
            for msg in reversed(self.messages):
                role = getattr(msg, "role", None)
                content = getattr(msg, "content", None)
                if role == "user" and isinstance(content, str):
                    self.query = content.strip()
                    break
        if not self.query:
            raise ValueError("Either 'query' or 'messages' (with a user message) is required.")
        return self


@router.post(
    "/compat/perplexity",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Compatibility search — returns snippets only",
    description="Compatibility endpoint for Open WebUI integration. Returns search snippets without fetching or synthesis. Hidden from OpenAPI spec — agents should use /v1/retrieve instead.",
    operation_id="search_perplexity_compat",
    include_in_schema=False,
)
async def compat_perplexity(
    body: PerplexityQuery,
    client: Annotated[LiteLLMSearchClient, Depends(get_litellm_client)],
) -> SearchResponse:
    """Compatibility endpoint for Open WebUI / Perplexity clients.

    Returns search result snippets only (no fetch, no synthesis).
    Accepts either ``{"query": "..."}`` or a full Perplexity shape with
    ``messages[]`` (query auto-extracted from the last user message).
    """
    log.info(
        "/compat/perplexity relay query='%s' max_results=%d",
        body.query,
        body.max_results,
    )
    return await client.search(query=body.query, max_results=body.max_results)


@router.post(
    "/v1/search",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    summary="OpenAI-compatible search alias",
    operation_id="search_v1",
    include_in_schema=False,
)
async def openai_search_alias(
    body: PerplexityQuery,
    client: Annotated[LiteLLMSearchClient, Depends(get_litellm_client)],
) -> SearchResponse:
    """Alias for /compat/perplexity — same request and response shape.

    Provided for clients expecting an OpenAI-style ``/v1/search`` endpoint.
    """
    log.info(
        "/v1/search alias query='%s' max_results=%d",
        body.query,
        body.max_results,
    )
    return await client.search(query=body.query, max_results=body.max_results)
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from app.dependencies import get_litellm_client
from app.services.litellm_search import LiteLLMSearchClient, SearchResponse

log = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["search"])


class PerplexityQuery(BaseModel):
    """Request body for /compat/perplexity."""

    query: str = Field(..., description="Search query string")
    max_results: int = Field(default=10, ge=1, le=100, description="Maximum results to return")


@router.post(
    "/compat/perplexity",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Perplexity-compatible search",
)
async def compat_perplexity(
    body: PerplexityQuery,
    client: Annotated[LiteLLMSearchClient, Depends(get_litellm_client)],
) -> SearchResponse:
    """Thin relay to LiteLLM search router.

    Request shape is Perplexity-compatible; response shape matches
    ``{"results": [{"title": "", "url": "", "snippet": ""}]}``.
    """
    log.info(
        "/compat/perplexity relay query='%s' max_results=%d",
        body.query,
        body.max_results,
    )
    return await client.search(query=body.query, max_results=body.max_results)
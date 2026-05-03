"""Fetch router — thin wrapper around FetchChain for the /fetch endpoint."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.dependencies import get_fetch_chain
from app.services.models import FetchResult
from app.services.fetch_chain import FetchChain

log = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["fetch"])


class FetchRequest(BaseModel):
    """Request body for the /fetch endpoint."""

    url: str = Field(..., description="URL to fetch")


@router.post(
    "/fetch",
    response_model=FetchResult,
    status_code=status.HTTP_200_OK,
    summary="Fetch a URL via multi-tier chain",
)
async def fetch_url(
    body: FetchRequest,
    format: Annotated[str, Query(description="Response format (markdown/text/html), future use")] = "markdown",
    chain: Annotated[FetchChain, Depends(get_fetch_chain)] = None,  # type: ignore[assignment]
) -> FetchResult:
    """Fetch a URL through the tiered chain: Crawl4AI → Jina Reader → anti-bot firebreak.

    Returns markdown (or raw HTML for anti-bot bypass results) with metadata.
    Set ``format=html`` for future raw HTML support.
    """
    log.info("/fetch url='%s' format=%s", body.url, format)
    result = await chain.execute(body.url)
    return result
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
    summary="Fetch content from a specific URL",
    operation_id="fetch_url",
)
async def fetch_url(
    body: FetchRequest,
    format: Annotated[str, Query(description="Response format: markdown (default; currently the only supported format). Future: text, html.")] = "markdown",
    chain: Annotated[FetchChain, Depends(get_fetch_chain)] = None,  # type: ignore[assignment]
) -> FetchResult:
    """Use this tool when the user provides a specific URL and asks you to read,
    summarize, quote, or analyze that page. Fetches through a tiered chain
    (Crawl4AI → Jina Reader → anti-bot fallback) and returns markdown content
    with metadata.

    Set ``format=html`` for future raw HTML support.
    """
    log.info("/fetch url='%s' format=%s", body.url, format)
    result = await chain.execute(body.url)
    return result
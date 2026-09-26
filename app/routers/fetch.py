"""Fetch router — thin wrapper around FetchChain for the /fetch endpoint."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies import get_fetch_chain
from app.schemas import FetchRequest
from app.services.fetch_chain import FetchChain
from app.services.models import FetchResult

log = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["fetch"])


FETCH_DESCRIPTION = """\
Fetch the full content of a specific URL as markdown.

Use this when the user provides a **specific URL** and asks you to read,
summarize, quote, or analyze that page's content. The fetch chain tries
Crawl4AI (headless browser) first, then Jina Reader, then anti-bot fallbacks
(Scrape.do → ScraperAPI).

**When to choose this endpoint:**
- User says "read this page" or "what does this article say" with a URL
- You need the full text of a known URL for summarization or extraction
- You want to verify or quote a specific source

**When NOT to use this endpoint:**
- For searching the web or researching topics — use ``/v1/retrieve`` instead
"""

@router.post(
    "/fetch",
    response_model=FetchResult,
    status_code=status.HTTP_200_OK,
    summary="Fetch content from a specific URL",
    description=FETCH_DESCRIPTION,
    operation_id="fetch_url",
)
async def fetch_url(
    body: FetchRequest,
    format: Annotated[
        str,
        Query(
            description=(
                "Response format: markdown (default; currently the only supported format). "
                "Future: text, html."
            )
        ),
    ] = "markdown",
    chain: Annotated[FetchChain, Depends(get_fetch_chain)] = None,  # type: ignore[assignment]
) -> FetchResult:
    """Use this tool when the user provides a specific URL and asks you to read,
    summarize, quote, or analyze that page. Fetches through a tiered chain
    (Crawl4AI → Jina Reader → anti-bot fallback) and returns markdown content
    with metadata.
    """
    log.info(
        "/fetch url='%s' format=%s actions=%s screenshot=%s",
        body.url,
        format,
        bool(body.actions),
        body.screenshot,
    )
    if body.actions or body.screenshot:
        result = await chain.execute(
            body.url,
            actions=body.actions,
            screenshot=body.screenshot,
        )
    else:
        result = await chain.execute(body.url)
    return result

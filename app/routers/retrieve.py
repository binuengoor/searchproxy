""" /v1/retrieve — search → rerank → fetch → synthesize pipeline."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse

from app.dependencies import get_retrieve_service
from app.schemas import RetrieveRequest, RetrieveResponse
from app.services.retrieve_service import RetrieveService

log = logging.getLogger(__name__)
router = APIRouter(tags=["retrieve"])


RETRIEVE_DESCRIPTION = """\
One-shot research endpoint: search → rerank → fetch → synthesize.

Use this when you need a **cited, factual answer** from the web — not just 
links or snippets, but a synthesized response that cross-references multiple 
sources with inline [N] citations.

**When to choose this endpoint:**
- User asks a question that requires looking up current information and getting
  an accurate, sourced answer — e.g. "What is the latest on X?", "Compare A vs B"
- You need sources cited inline in the answer, not just a list of links
- You want faster (5–15s) results than deep research but more depth than a 
  simple search

**When NOT to use this endpoint:**
- For quick factual lookups (a single fact, definition, or spelling) — use 
  `/compat/perplexity` instead (faster, returns snippets)
- For deep, multi-source analytical research (literature reviews, comprehensive
  reports) — use `/vane` instead (slower, produces a full report)
- To read a specific URL the user already has — use `/fetch` instead

**Pipeline:** LiteLLM search → BGE rerank → Crawl4AI/Jina/anti-bot fetch → 
LLM synthesis with citation prompt.

**Streaming:** Set `stream: true` to receive the LLM synthesis as SSE tokens.
Search/rerank/fetch phases are still synchronous; only synthesis streams.
"""

@router.post(
    "/v1/retrieve",
    response_model=RetrieveResponse,
    status_code=status.HTTP_200_OK,
    summary="Research a topic: search, rerank, fetch sources, synthesize a cited answer",
    description=RETRIEVE_DESCRIPTION,
    operation_id="retrieve",
)
async def retrieve(
    body: RetrieveRequest,
    service: Annotated[RetrieveService, Depends(get_retrieve_service)],
) -> RetrieveResponse | StreamingResponse:
    """One-shot research endpoint. Searches the web, reranks results,
    fetches top sources, and synthesizes a cited answer.

    Returns a structured response with inline [N] citations and source URLs.
    Set ``synthesize`` to false to skip LLM synthesis and get raw source chunks.
    Set ``stream`` to true to receive the LLM synthesis as SSE tokens.
    """
    log.info(
        "/v1/retrieve query='%s' max_results=%d fetch_top_k=%d synthesize=%s stream=%s",
        body.query,
        body.max_results,
        body.fetch_top_k,
        body.synthesize,
        body.stream,
    )

    if body.stream and body.synthesize:
        return StreamingResponse(
            service.retrieve_stream(
                query=body.query,
                max_results=body.max_results,
                fetch_top_k=body.fetch_top_k,
            ),
            media_type="text/event-stream",
        )

    return await service.retrieve(
        query=body.query,
        max_results=body.max_results,
        fetch_top_k=body.fetch_top_k,
        synthesize=body.synthesize,
    )

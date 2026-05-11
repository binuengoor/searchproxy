""" /v1/retrieve — search → rerank → fetch → synthesize pipeline."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import StreamingResponse

from app.dependencies import get_retrieve_service
from app.schemas import RetrieveRequest, RetrieveResponse
from app.services.retrieve_service import RetrieveService

log = logging.getLogger(__name__)
router = APIRouter(tags=["retrieve"])


RETRIEVE_DESCRIPTION = """\
Search the web and return a cited, synthesized answer.

The primary and recommended search tool for any question needing current,
sourced information. Works in one shot: searches, reranks for relevance,
fetches the top sources, and synthesizes a concise answer with inline [N]
citations and source URLs.

**What it handles well:**
- Factual questions ("What is the latest on X?", "When did Y happen?")
- Comparisons ("Compare A vs B")
- Research topics ("What do experts say about X?")
- Any question where you need a cited answer, not just links

**Latency:** 5–15s for typical queries.

**Streaming:** Set ``stream: true`` to receive source metadata progressively
as each fetch completes, followed by real-time LLM synthesis tokens.

**Parameters:**
- ``max_results``: Search result pool size (default 10, max 50)
- ``fetch_top_k``: How many results to fetch content from (default 5, max 10).
  Use lower values (2–3) for quick lookups, higher (8–10) for thorough research
- ``synthesize``: Set to ``false`` to skip LLM synthesis and get raw source chunks
- ``stream``: Set to ``true`` for SSE streaming (incremental source events + tokens)
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
    request: Request,
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
                request=request,
            ),
            media_type="text/event-stream",
        )

    return await service.retrieve(
        query=body.query,
        max_results=body.max_results,
        fetch_top_k=body.fetch_top_k,
        synthesize=body.synthesize,
        request=request,
    )

"""/v1/retrieve — search → rerank → fetch → synthesize pipeline."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.dependencies import get_retrieve_service
from app.schemas import RetrieveRequest, RetrieveResponse
from app.services.retrieve_service import RetrieveService

log = logging.getLogger(__name__)
router = APIRouter(tags=["retrieve"])


@router.post(
    "/v1/retrieve",
    response_model=RetrieveResponse,
    status_code=status.HTTP_200_OK,
    summary="Research a topic: search, fetch sources, synthesize an answer",
    operation_id="retrieve",
)
async def retrieve(
    body: RetrieveRequest,
    service: Annotated[RetrieveService, Depends(get_retrieve_service)],
) -> RetrieveResponse:
    """One-shot research endpoint. Searches the web, reranks results,
    fetches top sources, and synthesizes a cited answer.

    Returns a structured response with inline [N] citations and source URLs.
    Set ``synthesize`` to false to skip LLM synthesis and get raw source chunks.
    """
    log.info(
        "/v1/retrieve query='%s' max_results=%d fetch_top_k=%d synthesize=%s",
        body.query,
        body.max_results,
        body.fetch_top_k,
        body.synthesize,
    )
    return await service.retrieve(
        query=body.query,
        max_results=body.max_results,
        fetch_top_k=body.fetch_top_k,
        synthesize=body.synthesize,
    )
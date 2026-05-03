from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.main import get_client
from app.services.vane_proxy import VaneProxyClient, VaneResearchResponse

log = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["vane"])


class VaneRequest(BaseModel):
    """Request body for the /vane endpoint."""

    query: str = Field(..., description="Research query or topic")
    depth: str = Field(
        default="balanced",
        description="Research depth: 'concise', 'balanced', or 'comprehensive'",
    )


def _get_vane_client() -> VaneProxyClient:
    """DI helper: build a VaneProxyClient from shared infrastructure."""
    from app.config import settings

    return VaneProxyClient(client=get_client(), settings=settings)


@router.post(
    "/vane",
    response_model=VaneResearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Vane deep research proxy",
)
async def vane_research(
    body: VaneRequest,
    stream: Annotated[bool, Query(description="Enable streaming response")] = False,
    client: Annotated[VaneProxyClient, Depends(_get_vane_client)] = None,  # type: ignore[assignment]
) -> VaneResearchResponse | StreamingResponse:
    """Proxy to Vane deep research service.

    Accepts a research query and returns a synthesized report with inline
    citations. Set ``stream=true`` to receive the report as a streaming
    text response.
    """
    log.info("/vane query='%s' depth=%s stream=%s", body.query, body.depth, stream)

    if stream:
        return StreamingResponse(
            client.research_stream(query=body.query, depth=body.depth),
            media_type="text/plain; charset=utf-8",
        )

    report = await client.research(query=body.query, depth=body.depth)
    return VaneResearchResponse(report=report)

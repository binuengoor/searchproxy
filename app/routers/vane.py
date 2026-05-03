from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.dependencies import get_vane_client
from app.services.vane_proxy import VaneProxyClient, VaneResearchResponse, VaneTimeoutError, VaneUpstreamError, VaneError

log = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["vane"])


class VaneRequest(BaseModel):
    """Request body for the /vane endpoint."""

    query: str = Field(..., description="Research query or topic")
    optimization_mode: str = Field(
        default="balanced",
        description="Research depth: 'speed', 'balanced', or 'quality'",
    )


@router.post(
    "/vane",
    response_model=VaneResearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Vane deep research proxy",
)
async def vane_research(
    body: VaneRequest,
    stream: Annotated[bool, Query(description="Enable streaming response")] = False,
    client: Annotated[VaneProxyClient, Depends(get_vane_client)] = None,  # type: ignore[assignment]
) -> VaneResearchResponse | StreamingResponse:
    """Proxy to Vane deep research service.

    Accepts a research query and returns a synthesized report with inline
    citations. Set ``stream=true`` to receive the report as a streaming
    text response.
    """
    log.info("/vane query='%s' optimization_mode=%s stream=%s", body.query, body.optimization_mode, stream)

    if stream:
        return StreamingResponse(
            client.research_stream(query=body.query, optimization_mode=body.optimization_mode),
            media_type="text/plain; charset=utf-8",
        )

    try:
        report = await client.research(query=body.query, optimization_mode=body.optimization_mode)
    except VaneTimeoutError as exc:
        log.error("Vane timeout: %s", exc)
        return VaneResearchResponse(report=f"[Deep research unavailable: {exc}]")
    except VaneUpstreamError as exc:
        log.error("Vane upstream error: %s", exc)
        return VaneResearchResponse(report=f"[Deep research unavailable: {exc}]")
    except VaneError as exc:
        log.error("Vane error: %s", exc)
        return VaneResearchResponse(report=f"[Deep research unavailable: {exc}]")

    return VaneResearchResponse(report=report)
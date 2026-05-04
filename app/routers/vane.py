from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.dependencies import get_vane_client
from app.schemas import MessageItem
from app.services.vane_proxy import VaneProxyClient, VaneResearchResponse, VaneTimeoutError, VaneUpstreamError, VaneError

log = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["vane"])


class VaneRequest(BaseModel):
    """Request body for the /vane endpoint.

    Supports two shapes:

    - Simple (preferred): ``{\"query\": \"...\"}``
    - Open WebUI / Perplexity-compatible: ``{\"messages\": [{\"role\": \"user\", \"content\": \"...\"}]}``

    When ``messages`` is provided, the query is extracted from the **last**
    ``user`` message. All other Perplexity fields (``model``, ``stream``,
    ``return_related_questions``, ``search_recency_filter``) are accepted but
    ignored.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"query": "Real Madrid's 2025-26 La Liga season", "optimization_mode": "balanced"},
                {
                    "messages": [
                        {"role": "user", "content": "Explain how transformer attention works."}
                    ],
                    "optimization_mode": "quality",
                },
            ]
        }
    )

    query: str = Field(
        default="",
        description="Research query or topic. Mutually exclusive with ``messages`` — provide one or the other.",
    )
    optimization_mode: str = Field(
        default="balanced",
        json_schema_extra={"enum": ["speed", "balanced", "quality"]},
        description="Research depth: 'speed' (60s timeout), 'balanced' (180s), or 'quality' (300s)",
    )
    # —— Open WebUI / Perplexity compat fields (ignored on Vane) ——
    messages: list[MessageItem] = Field(
        default=[],
        description="OpenAI-style messages array. If provided, query is extracted from the last user message.",
    )
    stream: bool = Field(default=False, description="Ignored — forwarded for Open WebUI compat.")
    return_related_questions: bool = Field(
        default=False, description="Ignored — forwarded for Open WebUI compat."
    )
    search_recency_filter: str = Field(
        default="", description="Ignored — forwarded for Open WebUI compat."
    )

    @model_validator(mode="after")
    def _extract_query(self) -> "VaneRequest":
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
    "/vane",
    response_model=VaneResearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Deep research with synthesis and citations",
    operation_id="research_vane",
)
async def vane_research(
    body: VaneRequest,
    stream: Annotated[bool, Query(description="Enable streaming response")] = False,
    client: Annotated[VaneProxyClient, Depends(get_vane_client)] = None,  # type: ignore[assignment]
) -> VaneResearchResponse | StreamingResponse:
    """Use this tool for deep, complex, or analytical research questions that
    require synthesizing information across multiple sources. Produces a
    comprehensive report with inline citations. Slower than simple search but
    delivers a fully synthesized answer.

    Set ``optimization_mode`` to ``speed`` (60s timeout), ``balanced`` (180s),
    or ``quality`` (300s) to control depth vs. speed.

    Accepts either ``{\"query\": \"...\"}`` or a full Open WebUI / Perplexity
    shape with ``messages[]`` (query auto-extracted from the last user message).
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

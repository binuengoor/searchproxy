"""Deep research router — multi-hop query decomposition, parallel search & comprehensive reports."""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.dependencies import get_deep_research_service
from app.schemas import MessageItem, RetrieveResponse
from app.services.deep_research_service import DeepResearchService

log = logging.getLogger(__name__)
router = APIRouter(tags=["research"])

RESEARCH_DESCRIPTION = """\
Deep research tool that performs autonomous multi-hop query planning, parallel multi-source search, and comprehensive report synthesis.

**When to use this tool:**
- Broad, complex, or multi-faceted inquiries (e.g. market analysis, architectural comparisons, technical deep dives).
- When you need a structured report covering multiple angles with inline citations.

**How it works:**
1. **Query Decomposition:** Automatically expands the user query into 2–3 specialized sub-queries.
2. **Parallel Sub-Search:** Queries search providers in parallel across all angles.
3. **Neural Reranking & Diversity:** Merges candidates, applies domain diversity, and reranks via BGE.
4. **Deep Extraction & Synthesis:** Reads top candidate pages (using Crawl4AI, Byparr, Tika) and writes an in-depth cited report.

**Latency:** 10–25s.
"""


class ResearchRequest(BaseModel):
    """Request body for ``POST /v1/research``."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "query": "Compare Apple M4 Max vs Intel Core Ultra 9 285K for AI workloads and battery efficiency",
                    "fetch_top_k": 8,
                }
            ]
        }
    )

    query: str = Field(
        default="",
        description="Complex topic or question requiring multi-angle deep research.",
    )
    fetch_top_k: int = Field(
        default=8, ge=3, le=15,
        description="Number of diverse source documents to fetch and analyze.",
    )
    include_domains: list[str] = Field(
        default=[],
        description="Optional list of domains to restrict research to.",
    )
    exclude_domains: list[str] = Field(
        default=[],
        description="Optional list of domains to exclude from research.",
    )
    # Open WebUI tool wrapper compatibility
    body: Any = Field(default=None, description="Optional nested body wrapper from tool invocations.")
    messages: list[MessageItem] = Field(default=[], description="OpenAI-style messages array.")

    @model_validator(mode="before")
    @classmethod
    def _normalize_payload(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "body" in data and isinstance(data["body"], dict):
            for k in ("query", "fetch_top_k", "include_domains", "exclude_domains", "messages"):
                if k in data["body"] and (k not in data or not data[k]):
                    data[k] = data["body"][k]
        if not data.get("query") and data.get("messages"):
            for msg in reversed(data["messages"]):
                if isinstance(msg, dict) and msg.get("role") == "user" and msg.get("content"):
                    data["query"] = msg["content"].strip()
                    break
        return data


@router.post(
    "/v1/research",
    response_model=RetrieveResponse,
    status_code=status.HTTP_200_OK,
    summary="Deep multi-hop research with query decomposition and comprehensive cited report",
    description=RESEARCH_DESCRIPTION,
    operation_id="research",
)
async def deep_research(
    body: ResearchRequest,
    request: Request,
    service: Annotated[DeepResearchService, Depends(get_deep_research_service)],
) -> RetrieveResponse:
    """Execute autonomous 2-hop deep research and return a cited report."""
    log.info("/v1/research query='%s' fetch_top_k=%d", body.query, body.fetch_top_k)
    return await service.research(
        query=body.query,
        fetch_top_k=body.fetch_top_k,
        include_domains=body.include_domains,
        exclude_domains=body.exclude_domains,
        request=request,
    )


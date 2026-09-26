"""Extract router — Firecrawl-compatible structured JSON extraction endpoint."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.dependencies import get_extract_service
from app.schemas import ExtractRequest, ExtractResponse
from app.services.extract_service import ExtractService

log = logging.getLogger(__name__)
router = APIRouter(tags=["extract"])

EXTRACT_DESCRIPTION = """\
Extract structured JSON from a web page using a specified JSON schema and guidance prompt.

**How it works:**
1. **Scrapes** requested URL using multi-tier fetch chain (Crawl4AI, Jina, Byparr, Tika).
2. **Analyzes** page content with an LLM using JSON mode and schema constraints.
3. **Validates** the output against your supplied JSON Schema (if provided).
4. **Returns** clean, strongly-typed structured JSON.

**When to use:**
- Extracting specific fields (e.g., product prices, article metadata) from any web page.
- Firecrawl `/v1/extract` compatible workloads.
"""


@router.post(
    "/v1/extract",
    response_model=ExtractResponse,
    status_code=status.HTTP_200_OK,
    summary="Extract structured JSON from a URL using schema constraints",
    description=EXTRACT_DESCRIPTION,
    operation_id="extract_url",
)
async def extract(
    body: ExtractRequest,
    service: Annotated[ExtractService, Depends(get_extract_service)],
) -> ExtractResponse:
    """Scrape a URL and extract structured data conforming to a JSON schema."""
    log.info("/v1/extract url='%s' has_schema=%s", body.url, bool(body.schema_))
    return await service.extract(
        url=body.url,
        schema=body.schema_,
        prompt=body.prompt,
        system_prompt=body.system_prompt,
    )


@router.post(
    "/v1/extract/",
    response_model=ExtractResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def extract_trailing_slash(
    body: ExtractRequest,
    service: Annotated[ExtractService, Depends(get_extract_service)],
) -> ExtractResponse:
    """Trailing slash variant of /v1/extract."""
    return await extract(body=body, service=service)


@router.post(
    "/compat/firecrawl/v1/extract",
    response_model=ExtractResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def firecrawl_extract_v1(
    body: ExtractRequest,
    service: Annotated[ExtractService, Depends(get_extract_service)],
) -> ExtractResponse:
    """Firecrawl compatibility alias for /v1/extract."""
    return await extract(body=body, service=service)

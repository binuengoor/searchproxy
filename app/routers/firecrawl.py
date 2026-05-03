"""Firecrawl v2 scrape compat router.

Thin wrapper that exposes searchproxy's tiered fetch chain behind the
Firecrawl v2 /scrape API contract.

Unsupported parameters (actions, location, mobile, includeTags, excludeTags,
blockAds, proxy, storeInCache, zeroDataRetention, parsers) are accepted in the
request body but ignored with a log warning.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, HttpUrl

from app.services.crawl4ai import FetchResult
from app.services.fetch_chain import FetchChain
from app.services.firecrawl_compat import build_firecrawl_response

log = logging.getLogger(__name__)
router = APIRouter(prefix="/compat/firecrawl", tags=["firecrawl"])

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ScrapeRequest(BaseModel):
    """Firecrawl v2 /scrape request body.

    Only ``url`` is required. All other fields mirror Firecrawl's schema but
    most are no-ops because searchproxy does not run a headless browser.
    """

    url: HttpUrl = Field(..., description="URL to scrape")
    formats: list[str] = Field(default=["markdown"], description="Output formats")
    onlyMainContent: bool = Field(default=True, description="Ignored — Crawl4AI already extracts main content")
    includeTags: list[str] = Field(default=[], description="Ignored — no DOM-level filtering")
    excludeTags: list[str] = Field(default=[], description="Ignored — no DOM-level filtering")
    headers: dict[str, str] = Field(default={}, description="Ignored — fetch chain uses its own headers")
    waitFor: int = Field(default=0, description="Ignored — no headless browser")
    mobile: bool = Field(default=False, description="Ignored — no headless browser")
    skipTlsVerification: bool = Field(default=True, description="Ignored — httpx client already configured")
    timeout: int = Field(default=30000, ge=1000, le=300000, description="Scrape timeout in milliseconds")
    actions: list[dict[str, Any]] = Field(default=[], description="Ignored — no headless browser")
    location: dict[str, Any] | None = Field(default=None, description="Ignored — no geolocation support")
    removeBase64Images: bool = Field(default=True, description="Ignored — markdown output has no base64")
    blockAds: bool = Field(default=True, description="Ignored — no ad blocker")
    proxy: str | None = Field(default=None, description="Ignored — fetch chain has its own proxy fallback")
    storeInCache: bool = Field(default=True, description="Ignored — no server-side cache")
    zeroDataRetention: bool = Field(default=False, description="Ignored — no retention policy")


def _get_fetch_chain() -> FetchChain:
    """DI helper: build a FetchChain from shared infrastructure."""
    from app.config import settings
    from app.main import get_client

    return FetchChain(client=get_client(), settings=settings)


@router.post(
    "/v2/scrape",
    response_model=dict[str, Any],
    status_code=status.HTTP_200_OK,
    summary="Firecrawl v2-compatible scrape endpoint",
)
async def firecrawl_scrape(
    body: ScrapeRequest,
    chain: Annotated[FetchChain, Depends(_get_fetch_chain)] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Scrape a URL through the tiered fetch chain, returning Firecrawl v2 shape.

    Unsupported parameters are silently ignored so that existing Firecrawl
    clients do not need to change their request bodies.
    """
    url = str(body.url)
    timeout_ms = body.timeout

    # Log ignored params for observability
    ignored = []
    if body.actions:
        ignored.append("actions")
    if body.location:
        ignored.append("location")
    if body.mobile:
        ignored.append("mobile")
    if body.waitFor > 0:
        ignored.append("waitFor")
    if body.includeTags:
        ignored.append("includeTags")
    if body.excludeTags:
        ignored.append("excludeTags")
    if body.headers:
        ignored.append("headers")
    if body.proxy:
        ignored.append("proxy")
    if ignored:
        log.info("Firecrawl compat ignoring unsupported params for %s: %s", url, ", ".join(ignored))

    log.info("/compat/firecrawl/scrape url=%s timeout_ms=%s formats=%s", url, timeout_ms, body.formats)

    # NOTE: timeout is applied per-fetch-chain tier. The shared httpx client
    # has a 60s global timeout; per-request timeout override would require
    # threading a timeout parameter through FetchChain. For now we use the
    # chain default (FETCH_TIMEOUT seconds) and document the limitation.
    result = await chain.execute(url)
    return build_firecrawl_response(result)

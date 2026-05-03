from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import Field

from app.main import get_client
from app.services.litellm_search import LiteLLMSearchClient
from app.services.searxng_compat import SearxngCompatService, SearxngParams, SearxngResponse

log = logging.getLogger(__name__)
router = APIRouter(prefix="/compat", tags=["search"])


def _get_searxng_service() -> SearxngCompatService:
    """DI helper: build a SearxngCompatService from shared infrastructure."""
    from app.config import settings

    return SearxngCompatService(
        litellm_client=LiteLLMSearchClient(client=get_client(), settings=settings),
        http_client=get_client(),
        settings=settings,
    )


@router.get(
    "/searxng",
    response_model=SearxngResponse,
    status_code=status.HTTP_200_OK,
    summary="SearXNG-compatible search",
)
async def compat_searxng(
    q: str = Query(..., description="Search query"),
    categories: str | None = Query(default=None, description="Result category (e.g. images, videos)"),
    engines: str | None = Query(default=None, description="Specific search engines"),
    language: str | None = Query(default=None),
    pageno: int | None = Query(default=None, ge=1),
    time_range: str | None = Query(default=None),
    safesearch: int | None = Query(default=None, ge=0, le=2),
    autocomplete: str | None = Query(default=None),
    service: Annotated[SearxngCompatService, Depends(_get_searxng_service)] = None,
) -> SearxngResponse:
    """SearXNG JSON API compatibility endpoint.

    For ``categories=images`` or ``categories=videos`` (if ``SEARXNG_URL`` is
    configured): passthrough to upstream SearXNG.
    For all other queries: call LiteLLM search and normalize to SearXNG format.
    """
    log.info(
        "/compat/searxng q='%s' categories=%s engines=%s",
        q,
        categories,
        engines,
    )
    params = SearxngParams(
        q=q,
        categories=categories,
        engines=engines,
        language=language,
        pageno=pageno,
        time_range=time_range,
        safesearch=safesearch,
        autocomplete=autocomplete,
    )
    return await service.search(params)

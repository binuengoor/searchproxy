from __future__ import annotations

import logging
from typing import Annotated
from fastapi import APIRouter, Depends, Query, Response, status

from app.main import get_client
from app.services.litellm_search import LiteLLMSearchClient
from app.services.searxng_compat import MEDIA_CATEGORIES, SearxngCompatService, SearxngParams, SearxngResponse
from app.services.searxng_ui import render_html_results, upstream_html_forward

log = logging.getLogger(__name__)
router = APIRouter(prefix="/compat", tags=["search"])


# ── DI helper ────────────────────────────────────────────────────────────────

def _get_searxng_service() -> SearxngCompatService:
    """Build a SearxngCompatService from shared infrastructure."""
    from app.config import settings

    return SearxngCompatService(
        litellm_client=LiteLLMSearchClient(client=get_client(), settings=settings),
        http_client=get_client(),
        settings=settings,
    )


# ── Shared handler (both /compat/searxng and /compat/searxng/search) ─────────

async def _handle_searxng(
    *,
    q: str,
    categories: str | None,
    engines: str | None,
    language: str | None,
    pageno: int | None,
    time_range: str | None,
    safesearch: int | None,
    autocomplete: str | None,
    format: str,
    service: SearxngCompatService,
) -> SearxngResponse | Response:
    """Handle a SearXNG-compat request in either JSON or HTML format."""
    log.info(
        "/compat/searxng q='%s' format=%s categories=%s engines=%s",
        q, format, categories, engines,
    )

    params = SearxngParams(
        q=q, categories=categories, engines=engines, language=language,
        pageno=pageno, time_range=time_range, safesearch=safesearch,
        autocomplete=autocomplete,
    )

    # ── HTML mode -----------------------------------------------------------
    if format == "html":
        from app.config import settings

        # Media queries: passthrough raw HTML from upstream SearXNG
        if categories and categories in MEDIA_CATEGORIES and settings.SEARXNG_URL:
            return await upstream_html_forward(
                q=q, categories=categories, pageno=pageno,
                time_range=time_range, safesearch=safesearch,
                http_client=service._http, searxng_url=settings.SEARXNG_URL,
            )

        # Fallback: render simple HTML from JSON results
        result = await service.search(params)
        html = render_html_results(
            query=q, results=result.results,
            number_of_results=result.number_of_results,
        )
        return Response(content=html, media_type="text/html")

    # ── JSON mode (default) -------------------------------------------------
    return await service.search(params)


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get(
    "/searxng",
    response_model=None,
    status_code=status.HTTP_200_OK,
    summary="SearXNG-compatible search",
)
@router.get(
    "/searxng/search",
    response_model=None,
    summary="SearXNG-compatible search (Vane subpath)",
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
    format: str = Query(default="json", description="Response format: json or html"),
    service: Annotated[SearxngCompatService, Depends(_get_searxng_service)] = None,
) -> SearxngResponse | Response:
    """SearXNG JSON API compatibility endpoint.

    Supports both ``/compat/searxng`` and ``/compat/searxng/search`` for
    backward compatibility with Vane and other SearXNG consumers.

    For ``categories=images`` or ``categories=videos`` (if ``SEARXNG_URL`` is
    configured): passthrough to upstream SearXNG.
    For all other queries: call LiteLLM search and normalize to SearXNG format.

    The ``format`` parameter accepts ``json`` (default) or ``html``.
    """
    return await _handle_searxng(
        q=q, categories=categories, engines=engines, language=language,
        pageno=pageno, time_range=time_range, safesearch=safesearch,
        autocomplete=autocomplete, format=format, service=service,
    )
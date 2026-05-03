from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import Field

from app.main import get_client
from app.services.litellm_search import LiteLLMSearchClient
from app.services.searxng_compat import SearxngCompatService, SearxngParams, SearxngResponse, SearxngResult

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


# ── HTML template ───────────────────────────────────────────────────────────

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Search results for {query}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 960px; margin: 0 auto; padding: 2rem; }}
    .stat {{ color: #666; margin-bottom: 1rem; }}
    .result {{ margin-bottom: 1.5rem; border-bottom: 1px solid #eee; padding-bottom: 1rem; }}
    .result h3 {{ margin: 0 0 .25rem; }}
    .result .url {{ color: #006621; font-size: .875rem; margin: 0 0 .25rem; }}
    .result .content {{ color: #333; margin: 0 0 .5rem; line-height: 1.4; }}
    .result .engine {{ color: #888; font-size: .75rem; margin: 0; }}
  </style>
</head>
<body>
  <h1>Search results</h1>
  <p class="stat">Query: <strong>{query}</strong> · {count} results</p>
  {rows}
</body>
</html>"""


def _render_html_results(query: str, results: list[SearxngResult], number_of_results: int) -> str:
    """Build a simple HTML results page from a SearxngResponse."""
    rows = ""
    for r in results:
        rows += (
            f'<article class="result">\n'
            f'  <h3><a href="{r.url}">{r.title}</a></h3>\n'
            f'  <p class="url">{r.url}</p>\n'
            f'  <p class="content">{r.content}</p>\n'
            f'  <p class="engine">Source: {r.engine}</p>\n'
            f'</article>\n'
        )
    return _HTML_TEMPLATE.format(query=query, count=number_of_results, rows=rows)


async def _upstream_html_forward(
    *,
    q: str,
    categories: str | None,
    pageno: int | None,
    time_range: str | None,
    safesearch: int | None,
    http_client,
    settings,
) -> Response:
    """Forward media queries to upstream SearXNG in HTML mode; return raw HTML."""
    import urllib.parse

    upstream = str(settings.SEARXNG_URL).rstrip("/")
    if not upstream.endswith("/search"):
        upstream = f"{upstream}/search"

    query_params: dict[str, str | int] = {"q": q, "format": "html"}
    if categories:
        query_params["categories"] = categories
    if pageno is not None:
        query_params["pageno"] = pageno
    if time_range:
        query_params["time_range"] = time_range
    if safesearch is not None:
        query_params["safesearch"] = safesearch

    try:
        resp = await http_client.get(upstream, params=query_params)
        resp.raise_for_status()
        return Response(content=resp.text, media_type="text/html")
    except Exception as exc:
        log.warning("Upstream HTML passthrough failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Upstream SearXNG HTML request failed",
        ) from exc


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
        q,
        format,
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

    # ── HTML mode -----------------------------------------------------------
    if format == "html":
        from app.config import settings

        # Media queries (images/videos): passthrough raw HTML from upstream SearXNG
        _MEDIA_CATEGORIES = frozenset(("images", "videos"))
        if categories and categories in _MEDIA_CATEGORIES and settings.SEARXNG_URL:
            return await _upstream_html_forward(
                q=q,
                categories=categories,
                pageno=pageno,
                time_range=time_range,
                safesearch=safesearch,
                http_client=service._http,
                settings=settings,
            )

        # Fallback: render simple HTML from JSON results
        result = await service.search(params)
        html = _render_html_results(
            query=q,
            results=result.results,
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
        q=q,
        categories=categories,
        engines=engines,
        language=language,
        pageno=pageno,
        time_range=time_range,
        safesearch=safesearch,
        autocomplete=autocomplete,
        format=format,
        service=service,
    )

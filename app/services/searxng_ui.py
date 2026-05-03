"""SearXNG HTML rendering and upstream HTML passthrough.

All presentation logic for the SearXNG compat endpoint. The router
calls :func:`render_html_results` or :func:`upstream_html_forward` —
it never builds HTML itself.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException, Response, status

from app.services.searxng_compat import MEDIA_CATEGORIES, SearxngResult

log = logging.getLogger(__name__)

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


# ── Rendering helpers ────────────────────────────────────────────────────────

def _is_media_result(r: SearxngResult) -> bool:
    """Return True if the result carries image or video metadata."""
    return bool(getattr(r, "img_src", None) or getattr(r, "thumbnail_src", None))


def _render_media_result(r: SearxngResult) -> str:
    """Render a single image/video result as HTML with thumbnail."""
    img_url = getattr(r, "img_src", None) or getattr(r, "thumbnail_src", None) or ""
    meta_parts: list[str] = []
    if getattr(r, "resolution", None):
        meta_parts.append(f"Resolution: {r.resolution}")
    if getattr(r, "source", None):
        meta_parts.append(f"Source: {r.source}")
    meta_str = " · ".join(meta_parts) if meta_parts else ""
    return (
        f'<article class="result media">\n'
        f'  <img src="{img_url}" alt="{r.title}" loading="lazy" '
        f'       style="max-width:200px;max-height:200px;" '
        f'       onerror="this.style.display=\'none\'">\n'
        f'  <h3><a href="{r.url}">{r.title}</a></h3>\n'
        f'  <p class="url">{r.url}</p>\n'
        f'  <p class="meta">{meta_str}</p>\n'
        f'  <p class="engine">Source: {r.engine}</p>\n'
        f'</article>\n'
    )


def _render_web_result(r: SearxngResult) -> str:
    """Render a single general/web result as HTML."""
    return (
        f'<article class="result">\n'
        f'  <h3><a href="{r.url}">{r.title}</a></h3>\n'
        f'  <p class="url">{r.url}</p>\n'
        f'  <p class="content">{r.content}</p>\n'
        f'  <p class="engine">Source: {r.engine}</p>\n'
        f'</article>\n'
    )


def render_html_results(
    query: str,
    results: list[SearxngResult],
    number_of_results: int,
) -> str:
    """Build a simple HTML results page from :class:`SearxngResult` items.

    Renders media thumbnails when ``img_src`` or ``thumbnail_src`` are present.
    """
    rows = ""
    for r in results:
        rows += _render_media_result(r) if _is_media_result(r) else _render_web_result(r)
    return _HTML_TEMPLATE.format(query=query, count=number_of_results, rows=rows)


# ── Upstream passthrough ─────────────────────────────────────────────────────

async def upstream_html_forward(
    *,
    q: str,
    categories: str | None,
    pageno: int | None,
    time_range: str | None,
    safesearch: int | None,
    http_client: httpx.AsyncClient,
    searxng_url: str,
) -> Response:
    """Forward media queries to upstream SearXNG in HTML mode; return raw HTML."""
    upstream = str(searxng_url).rstrip("/")
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
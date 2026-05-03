"""Firecrawl v2 response mapper — pure formatting, no HTTP calls."""

from __future__ import annotations

from typing import Any

from app.services.crawl4ai import FetchResult


def build_firecrawl_response(result: FetchResult) -> dict[str, Any]:
    """Map a FetchResult to the Firecrawl v2 scrape response envelope.

    Firecrawl returns HTTP 200 in all cases; the ``success`` boolean inside
    the JSON body carries the actual outcome.
    """
    if result.success:
        return {
            "success": True,
            "data": {
                "markdown": result.markdown or None,
                "html": None,
                "metadata": {
                    "title": result.title or None,
                    "sourceURL": result.url,
                    "statusCode": result.status_code,
                },
            },
        }

    return {
        "success": False,
        "error": result.error or "Scrape failed",
    }

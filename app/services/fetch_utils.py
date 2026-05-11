"""Shared fetch utilities — eliminate duplicated try/except across fetch services.

Provides safe_fetch which wraps the common httpx error-handling pattern
(TimeoutException → HTTPStatusError → generic Exception) into a single
async function that always returns a FetchResult.
"""

from __future__ import annotations

import logging

import httpx

from app.services.models import FetchResult

log = logging.getLogger(__name__)


async def safe_fetch(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    source: str,
    *,
    timeout: httpx.Timeout,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
    check_403: bool = False,
) -> FetchResult:
    """Make an async HTTP request and return a FetchResult.

    Handles all common error paths (timeout, HTTP error, 403 anti-bot,
    unexpected exception) so callers never need to handle exceptions.

    Args:
        client: Shared httpx.AsyncClient.
        method: HTTP method ("GET" or "POST").
        url: Full URL to request.
        source: Source label for the FetchResult (e.g. "crawl4ai", "jina").
        timeout: httpx.Timeout for the request.
        json_body: JSON body for POST requests.
        headers: Extra request headers.
        check_403: If True, treat 403 responses as anti-bot blocks
            (return FetchResult with error="403 anti-bot block").

    Returns:
        FetchResult with success=True on 2xx, or success=False with
        appropriate error details on failure.
    """
    try:
        if method.upper() == "POST":
            response = await client.post(
                url, json=json_body, headers=headers, timeout=timeout,
            )
        else:
            response = await client.get(
                url, headers=headers, timeout=timeout,
            )
        status_code = response.status_code

        if check_403 and status_code == 403:
            log.warning("%s returned 403 for %s — anti-bot block", source, url)
            return FetchResult(
                success=False,
                url=url,
                error="403 anti-bot block",
                status_code=status_code,
                source=source,
            )

        response.raise_for_status()

        # Parse response body
        if method.upper() == "POST":
            data = response.json()
            if isinstance(data, dict):
                markdown = data.get("markdown", "")
                metadata = data.get("metadata", {}) or {}
                title = metadata.get("title", "") or ""
                description = metadata.get("description", "") or ""
                language = metadata.get("language", "") or ""
                return FetchResult(
                    success=True,
                    url=url,
                    markdown=markdown,
                    title=title,
                    description=description,
                    language=language,
                    status_code=status_code,
                    source=source,
                )
            # Non-dict JSON response
            return FetchResult(
                success=True,
                url=url,
                markdown=str(data),
                status_code=status_code,
                source=source,
            )
        else:
            # GET — response body is raw text (e.g. Jina markdown)
            return FetchResult(
                success=True,
                url=url,
                markdown=response.text,
                status_code=status_code,
                source=source,
            )

    except httpx.TimeoutException:
        log.warning("%s fetch timed out for %s", source, url)
        return FetchResult(
            success=False, url=url, error="timeout", source=source,
        )
    except httpx.HTTPStatusError as exc:
        log.warning("%s fetch returned HTTP %d for %s", source, exc.response.status_code, url)
        return FetchResult(
            success=False,
            url=url,
            error=f"HTTP {exc.response.status_code}",
            markdown=exc.response.text[:2000],
            status_code=exc.response.status_code,
            source=source,
        )
    except Exception as exc:
        log.warning("%s fetch failed for %s: %s", source, url, exc)
        return FetchResult(
            success=False, url=url, error=str(exc), source=source,
        )

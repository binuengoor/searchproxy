"""Crawl4AI service client — self-hosted primary fetcher."""

from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.services.models import FetchResult

log = logging.getLogger(__name__)


class Crawl4AIClient:
    """Standalone async client for the self-hosted Crawl4AI service.

    Does not reach into other services. Owns its own request logic.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._timeout = httpx.Timeout(
            timeout=float(settings.FETCH_TIMEOUT),
            connect=5.0,
        )

    async def fetch_markdown(self, url: str) -> FetchResult:
        """POST to Crawl4AI /md for plain markdown fetch.

        Graceful degradation: on any error (timeout, HTTP error, parse failure)
        returns ``FetchResult(success=False, ...)`` so callers always get a valid
        result and never need to handle exceptions.

        Args:
            url: The target URL to fetch.

        Returns:
            FetchResult with markdown content on success, or success=False on failure.
        """
        log.info("Crawl4AI fetch_markdown: %s", url)

        body: dict[str, str] = {"url": url, "filter": "fit"}

        try:
            response = await self._client.post(
                f"{self._settings.CRAWL4AI_URL}/md",
                json=body,
                timeout=self._timeout,
            )
            status_code = response.status_code

            if status_code == 403:
                log.warning("Crawl4AI returned 403 for %s — anti-bot block", url)
                return FetchResult(
                    success=False,
                    url=url,
                    error="403 anti-bot block",
                    status_code=status_code,
                    source="crawl4ai",
                )

            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            log.warning("Crawl4AI fetch_markdown timed out for %s", url)
            return FetchResult(
                success=False,
                url=url,
                error="timeout",
                source="crawl4ai",
            )
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Crawl4AI fetch_markdown returned HTTP %d for %s",
                exc.response.status_code,
                url,
            )
            return FetchResult(
                success=False,
                url=url,
                error=f"HTTP {exc.response.status_code}",
                markdown=exc.response.text[:2000],
                status_code=exc.response.status_code,
                source="crawl4ai",
            )
        except Exception as exc:
            log.warning("Crawl4AI fetch_markdown failed for %s: %s", url, exc)
            return FetchResult(
                success=False,
                url=url,
                error=str(exc),
                source="crawl4ai",
            )

        markdown = data.get("markdown", "") if isinstance(data, dict) else ""
        title = ""
        description = ""
        language = ""
        if isinstance(data, dict):
            metadata = data.get("metadata", {})
            if isinstance(metadata, dict):
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
            source="crawl4ai",
        )
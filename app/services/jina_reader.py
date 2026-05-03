"""Jina Reader service client — free cloud backup fetcher."""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.services.crawl4ai import FetchResult

log = logging.getLogger(__name__)


class JinaReaderClient:
    """Standalone async client for the Jina Reader service.

    Does not reach into other services. Owns its own request logic.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def fetch(self, url: str) -> FetchResult:
        """GET Jina Reader at https://r.jina.ai/http://{url}.

        Graceful degradation: on any error (timeout, HTTP error, parse failure)
        returns ``FetchResult(success=False, ...)`` so callers always get a valid
        result and never need to handle exceptions.

        Args:
            url: The target URL to fetch.

        Returns:
            FetchResult with markdown content on success, or success=False on failure.
        """
        log.info("Jina Reader fetch: %s", url)

        timeout = httpx.Timeout(
            timeout=float(self._settings.FETCH_TIMEOUT),
            connect=5.0,
        )

        jina_url = f"https://r.jina.ai/http://{url}"
        headers: dict[str, str] = {}
        if self._settings.JINA_API_KEY:
            headers["Authorization"] = f"Bearer {self._settings.JINA_API_KEY}"

        try:
            response = await self._client.get(
                jina_url,
                headers=headers,
                timeout=timeout,
            )
            status_code = response.status_code

            if status_code == 403:
                log.warning("Jina Reader returned 403 for %s — anti-bot block", url)
                return FetchResult(
                    success=False,
                    url=url,
                    error="403 anti-bot block",
                    status_code=status_code,
                    source="jina",
                )

            response.raise_for_status()
            # Jina Reader returns plain text markdown, not JSON
            markdown = response.text
        except httpx.TimeoutException:
            log.warning("Jina Reader fetch timed out for %s", url)
            return FetchResult(
                success=False,
                url=url,
                error="timeout",
                source="jina",
            )
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Jina Reader fetch returned HTTP %d for %s",
                exc.response.status_code,
                url,
            )
            return FetchResult(
                success=False,
                url=url,
                error=f"HTTP {exc.response.status_code}",
                status_code=exc.response.status_code,
                source="jina",
            )
        except Exception as exc:
            log.warning("Jina Reader fetch failed for %s: %s", url, exc)
            return FetchResult(
                success=False,
                url=url,
                error=str(exc),
                source="jina",
            )

        return FetchResult(
            success=True,
            url=url,
            markdown=markdown,
            status_code=status_code,
            source="jina",
        )

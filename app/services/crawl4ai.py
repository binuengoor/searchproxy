"""Crawl4AI service client — self-hosted primary fetcher."""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, Field

from app.config import Settings

log = logging.getLogger(__name__)


class FetchResult(BaseModel):
    """Result of a fetch operation across all tiers."""

    success: bool = Field(default=False)
    url: str = Field(default="")
    markdown: str = Field(default="")
    title: str = Field(default="")
    error: str = Field(default="")
    status_code: int | None = Field(default=None)
    source: str = Field(default="")  # which tier succeeded: "crawl4ai", "jina", "scrape_do", "scraperapi"


class Crawl4AIClient:
    """Standalone async client for the self-hosted Crawl4AI service.

    Does not reach into other services. Owns its own request logic.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

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

        timeout = httpx.Timeout(
            timeout=float(self._settings.FETCH_TIMEOUT),
            connect=5.0,
        )

        body: dict[str, str] = {"url": url, "filter": "fit"}

        try:
            response = await self._client.post(
                f"{self._settings.CRAWL4AI_URL}/md",
                json=body,
                timeout=timeout,
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
        if isinstance(data, dict):
            metadata = data.get("metadata", {})
            if isinstance(metadata, dict):
                title = metadata.get("title", "") or ""

        return FetchResult(
            success=True,
            url=url,
            markdown=markdown,
            title=title,
            status_code=status_code,
            source="crawl4ai",
        )

    async def fetch_extract(self, url: str, prompt: str | None = None) -> FetchResult:
        """POST to Crawl4AI /crawl with LLM extraction config.

        Only available when CRAWL4AI_LLM_PROVIDER, CRAWL4AI_LLM_BASE_URL, and
        CRAWL4AI_LLM_API_KEY are all configured. Otherwise returns a failure result.

        Args:
            url: The target URL to fetch.
            prompt: Optional extraction prompt for LLM-guided extraction.

        Returns:
            FetchResult with extracted content on success, or success=False if
            LLM config is missing or the request fails.
        """
        if not all(
            [
                self._settings.CRAWL4AI_LLM_PROVIDER,
                self._settings.CRAWL4AI_LLM_BASE_URL,
                self._settings.CRAWL4AI_LLM_API_KEY,
            ]
        ):
            log.info(
                "Crawl4AI fetch_extract skipped: LLM config not fully set for %s",
                url,
            )
            return FetchResult(
                success=False,
                url=url,
                error="Crawl4AI LLM extraction not configured",
                source="crawl4ai",
            )

        log.info("Crawl4AI fetch_extract: %s", url)

        timeout = httpx.Timeout(
            timeout=float(self._settings.FETCH_TIMEOUT),
            connect=5.0,
        )

        extraction_config: dict[str, object] = {
            "provider": self._settings.CRAWL4AI_LLM_PROVIDER,
            "api_key": self._settings.CRAWL4AI_LLM_API_KEY,
            "base_url": self._settings.CRAWL4AI_LLM_BASE_URL,
        }
        if prompt:
            extraction_config["prompt"] = prompt

        body: dict[str, object] = {
            "url": url,
            "extraction_config": extraction_config,
        }

        try:
            response = await self._client.post(
                f"{self._settings.CRAWL4AI_URL}/crawl",
                json=body,
                timeout=timeout,
            )
            status_code = response.status_code
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            log.warning("Crawl4AI fetch_extract timed out for %s", url)
            return FetchResult(
                success=False,
                url=url,
                error="timeout",
                source="crawl4ai",
            )
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Crawl4AI fetch_extract returned HTTP %d for %s",
                exc.response.status_code,
                url,
            )
            return FetchResult(
                success=False,
                url=url,
                error=f"HTTP {exc.response.status_code}",
                status_code=exc.response.status_code,
                source="crawl4ai",
            )
        except Exception as exc:
            log.warning("Crawl4AI fetch_extract failed for %s: %s", url, exc)
            return FetchResult(
                success=False,
                url=url,
                error=str(exc),
                source="crawl4ai",
            )

        markdown = data.get("markdown", "") if isinstance(data, dict) else ""
        title = ""
        if isinstance(data, dict):
            metadata = data.get("metadata", {})
            if isinstance(metadata, dict):
                title = metadata.get("title", "") or ""

        return FetchResult(
            success=True,
            url=url,
            markdown=markdown,
            title=title,
            status_code=status_code,
            source="crawl4ai",
        )

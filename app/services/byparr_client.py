"""Byparr / FlareSolverr client for automated Cloudflare Turnstile/WAF bypass."""
from __future__ import annotations

import logging
from typing import Any
import httpx

from app.config import Settings
from app.services.content_cleaner import clean_content
from app.services.models import FetchResult

log = logging.getLogger(__name__)


class ByparrClient:
    """Automated Cloudflare and bot challenge solver using Byparr or FlareSolverr.

    Graceful degradation: Returns FetchResult(success=False) on connection error
    or solver failure without raising exceptions.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._url = settings.BYPARR_URL
        self._timeout = httpx.Timeout(
            timeout=float(settings.BYPARR_TIMEOUT),
            connect=self._settings.CONNECT_TIMEOUT,
        )

    def is_configured(self) -> bool:
        return bool(self._url and self._url.strip())

    async def fetch(self, url: str) -> FetchResult:
        if not self.is_configured():
            return FetchResult(success=False, url=url, error="Byparr URL not configured", source="byparr")

        log.info("Byparr attempting Cloudflare challenge solve for %s", url)
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": int(self._settings.BYPARR_TIMEOUT * 1000),
        }

        try:
            resp = await self._client.post(
                self._url,  # type: ignore[arg-type]
                json=payload,
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                return FetchResult(
                    success=False,
                    url=url,
                    status_code=resp.status_code,
                    error=f"Byparr HTTP {resp.status_code}",
                    source="byparr",
                )

            data = resp.json()
            status = data.get("status")
            if status != "ok":
                return FetchResult(
                    success=False,
                    url=url,
                    error=f"Byparr status: {data.get('message', status)}",
                    source="byparr",
                )

            solution = data.get("solution", {})
            html_content = solution.get("response", "")
            if not html_content:
                return FetchResult(
                    success=False,
                    url=url,
                    error="Byparr returned empty response HTML",
                    source="byparr",
                )

            markdown = clean_content(html_content, url=url, aggressive=True)
            log.info("Byparr solved challenge for %s — extracted %d chars", url, len(markdown))
            return FetchResult(
                success=True,
                url=url,
                status_code=solution.get("status", 200),
                content=markdown,
                title="",
                content_length=len(markdown),
                source="byparr",
            )
        except httpx.TimeoutException:
            log.warning("Byparr challenge solve timed out for %s", url)
            return FetchResult(success=False, url=url, error="Byparr solver timed out", source="byparr")
        except Exception as exc:
            log.warning("Byparr fetch failed for %s: %s", url, exc)
            return FetchResult(success=False, url=url, error=str(exc), source="byparr")

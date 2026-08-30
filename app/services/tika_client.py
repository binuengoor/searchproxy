"""Apache Tika document parser client for PDF and binary document extraction."""
from __future__ import annotations

import logging
import httpx

from app.config import Settings
from app.services.models import FetchResult

log = logging.getLogger(__name__)


class TikaClient:
    """Document text extraction client for Apache Tika."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._url = settings.TIKA_URL
        self._timeout = httpx.Timeout(
            timeout=float(settings.TIKA_TIMEOUT),
            connect=self._settings.CONNECT_TIMEOUT,
        )

    def is_configured(self) -> bool:
        return bool(self._url and self._url.strip())

    async def parse_bytes(self, raw_bytes: bytes, url: str) -> FetchResult:
        """Send raw document bytes to Apache Tika for plaintext extraction."""
        if not self.is_configured():
            return FetchResult(success=False, url=url, error="Tika URL not configured", source="tika")

        try:
            resp = await self._client.put(
                self._url,  # type: ignore[arg-type]
                content=raw_bytes,
                headers={"Accept": "text/plain"},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                return FetchResult(
                    success=False,
                    url=url,
                    status_code=resp.status_code,
                    error=f"Tika HTTP {resp.status_code}",
                    source="tika",
                )

            text = resp.text.strip()
            return FetchResult(
                success=bool(text),
                url=url,
                status_code=200,
                markdown=text,
                content_length=len(text),
                source="tika",
            )
        except Exception as exc:
            log.warning("Tika extraction failed for %s: %s", url, exc)
            return FetchResult(success=False, url=url, error=str(exc), source="tika")

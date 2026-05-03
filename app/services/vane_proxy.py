from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, Field

from app.config import Settings

log = logging.getLogger(__name__)


class VaneResearchResponse(BaseModel):
    """Response shape for the non-streaming /vane endpoint."""

    report: str = Field(default="", description="Synthesized research report text with inline citations.")


# Map user-facing depth names to Vane optimizationMode values.
_DEPTH_MAP = {
    "concise": "speed",
    "balanced": "balanced",
    "comprehensive": "quality",
}


class VaneProxyClient:
    """Standalone async client for the Vane deep research service.

    Does not reach into other services. Owns its own request logic.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def _build_url(self) -> str:
        """Return the Vane /api/search endpoint from the configured base URL."""
        base = str(self._settings.VANE_URL).rstrip("/")
        return f"{base}/api/search"

    def _build_body(self, query: str, depth: str, stream: bool) -> dict:
        """Build the Vane /api/search JSON body."""
        return {
            "query": query,
            "chatModel": {
                "providerId": self._settings.VANE_CHAT_PROVIDER_ID or "",
                "key": self._settings.VANE_CHAT_MODEL_KEY or "",
            },
            "embeddingModel": {
                "providerId": self._settings.VANE_EMBED_PROVIDER_ID or "",
                "key": self._settings.VANE_EMBED_MODEL_KEY or "",
            },
            "optimizationMode": _DEPTH_MAP.get(depth, depth),
            "sources": ["web"],
            "history": [],
            "stream": stream,
        }

    async def research(self, query: str, depth: str = "balanced") -> str:
        """POST to Vane /api/search, return the synthesized report as raw text.

        Graceful degradation: on any error (timeout, HTTP error, parse failure)
        returns an empty string so callers always get a valid response and never
        need to handle exceptions.

        Args:
            query: The research question or topic.
            depth: One of "concise", "balanced", "comprehensive".

        Returns:
            The report text (markdown with inline citations), or "" on failure.
        """
        log.info("Vane research request: query='%s' depth=%s", query, depth)

        body = self._build_body(query, depth, stream=False)
        timeout = httpx.Timeout(
            timeout=float(self._settings.VANE_TIMEOUT),
            connect=10.0,
        )

        try:
            response = await self._client.post(
                self._build_url(),
                json=body,
                timeout=timeout,
            )
            response.raise_for_status()
            # Vane returns JSON with {"message": "...", "sources": [...]}
            data = response.json()
            return data.get("message", "")
        except httpx.TimeoutException:
            log.warning("Vane research timed out for query='%s'", query)
            return ""
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Vane research returned HTTP %d for query='%s'",
                exc.response.status_code,
                query,
            )
            return ""
        except Exception as exc:
            log.warning("Vane research failed for query='%s': %s", query, exc)
            return ""

    async def research_stream(self, query: str, depth: str = "balanced"):
        """Yield report chunks from Vane as a streaming response.

        Uses ``httpx.AsyncClient.stream()`` to stream the request body.
        Yields raw text chunks as they arrive.

        Args:
            query: The research question or topic.
            depth: One of "concise", "balanced", "comprehensive".

        Yields:
            Raw text chunks from the Vane service.
        """
        log.info("Vane research stream request: query='%s' depth=%s", query, depth)

        body = self._build_body(query, depth, stream=True)
        timeout = httpx.Timeout(
            timeout=float(self._settings.VANE_TIMEOUT),
            connect=10.0,
        )

        try:
            async with self._client.stream(
                "POST",
                self._build_url(),
                json=body,
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_text():
                    if chunk:
                        yield chunk
        except httpx.TimeoutException:
            log.warning("Vane research stream timed out for query='%s'", query)
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Vane research stream returned HTTP %d for query='%s'",
                exc.response.status_code,
                query,
            )
        except Exception as exc:
            log.warning("Vane research stream failed for query='%s': %s", query, exc)

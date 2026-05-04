"""Standalone async client for the Vane deep research service.

Raises structured exceptions on failure — callers decide how to handle errors.
"""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings

log = logging.getLogger(__name__)


class VaneError(Exception):
    """Base exception for Vane service failures."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class VaneTimeoutError(VaneError):
    """Vane did not respond within the configured timeout."""


class VaneUpstreamError(VaneError):
    """Vane returned a non-2xx HTTP status."""


class VaneResearchResponse(BaseModel):
    """Response shape for the non-streaming /vane endpoint."""

    report: str = Field(default="", description="Synthesized research report text with inline citations.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "report": "## Real Madrid's 2025-26 La Liga Season\n\n" \
                               "Real Madrid started the 2025-26 La Liga campaign with a series of dominant victories...\n\n" \
                               "*Sources: ESPN, BBC Sport*"
                }
            ]
        }
    )


class VaneProxyClient:
    """Standalone async client for the Vane deep research service.

    Does not reach into other services. Owns its own request logic.
    Raises VaneError subclasses on failure — callers decide how to handle.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    # Timeouts scale with research depth
    _OPTIMIZATION_TIMEOUTS = {
        "speed": 60,
        "balanced": 180,
        "quality": 300,
    }

    def _timeout_for_mode(self, optimization_mode: str) -> httpx.Timeout:
        """Return an httpx.Timeout scaled to the optimization mode.

        Falls back to VANE_TIMEOUT env var if the mode is unknown.
        """
        timeout_seconds = self._OPTIMIZATION_TIMEOUTS.get(
            optimization_mode, self._settings.VANE_TIMEOUT
        )
        return httpx.Timeout(timeout=float(timeout_seconds), connect=10.0)

    def _build_url(self) -> str:
        """Return the Vane /api/search endpoint from the configured base URL."""
        base = str(self._settings.VANE_URL).rstrip("/")
        return f"{base}/api/search"

    def _build_body(self, query: str, optimization_mode: str, stream: bool) -> dict:
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
            "optimizationMode": optimization_mode,
            "sources": ["web"],
            "history": [],
            "stream": stream,
        }

    async def research(self, query: str, optimization_mode: str = "balanced") -> str:
        """POST to Vane /api/search, return the synthesized report as raw text.

        Args:
            query: The research question or topic.
            optimization_mode: One of "speed", "balanced", "quality".

        Returns:
            The report text (markdown with inline citations).

        Raises:
            VaneTimeoutError: Vane did not respond within the configured timeout.
            VaneUpstreamError: Vane returned a non-2xx status code.
            VaneError: Unexpected failure (connection, parse, etc.).
        """
        log.info("Vane research request: query='%s' optimization_mode=%s", query, optimization_mode)

        body = self._build_body(query, optimization_mode, stream=False)
        timeout = self._timeout_for_mode(optimization_mode)
        try:
            response = await self._client.post(
                self._build_url(),
                json=body,
                timeout=timeout,
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            raise VaneTimeoutError(
                f"Vane research timed out after {timeout.timeout}s for query='{query}'"
            ) from None
        except httpx.HTTPStatusError as exc:
            raise VaneUpstreamError(
                f"Vane returned HTTP {exc.response.status_code} for query='{query}'",
                status_code=exc.response.status_code,
            ) from None
        except httpx.RequestError as exc:
            raise VaneError(f"Vane request failed for query='{query}': {exc}") from exc

        data = response.json()
        return data.get("message", "")

    async def research_stream(self, query: str, optimization_mode: str = "balanced"):
        """Yield report chunks from Vane as a streaming response.

        Uses ``httpx.AsyncClient.stream()`` to stream the request body.
        Yields raw text chunks as they arrive. On failure, yields a single
        error chunk so streaming consumers receive a clear signal.

        Args:
            query: The research question or topic.
            optimization_mode: One of "speed", "balanced", "quality".

        Yields:
            Raw text chunks from the Vane service.
        """
        log.info("Vane research stream request: query='%s' optimization_mode=%s", query, optimization_mode)

        body = self._build_body(query, optimization_mode, stream=True)
        timeout = self._timeout_for_mode(optimization_mode)
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
            yield "[Vane stream error: timeout]"
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Vane research stream returned HTTP %d for query='%s'",
                exc.response.status_code,
                query,
            )
            yield f"[Vane stream error: HTTP {exc.response.status_code}]"
        except Exception as exc:
            log.warning("Vane research stream failed for query='%s': %s", query, exc)
            yield f"[Vane stream error: {exc}]"
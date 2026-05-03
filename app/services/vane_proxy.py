from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, Field

from app.config import Settings

log = logging.getLogger(__name__)


class VaneResearchResponse(BaseModel):
    """Response shape for the non-streaming /vane endpoint."""

    report: str = Field(default="", description="Synthesized research report text with inline citations.")


class VaneProxyClient:
    """Standalone async client for the Vane deep research service.

    Does not reach into other services. Owns its own request logic.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def research(self, query: str, depth: str = "balanced") -> str:
        """POST to Vane, return the synthesized report as raw text.

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

        body: dict[str, str] = {
            "message": query,
            "chatProviderId": self._settings.VANE_CHAT_PROVIDER_ID or "",
            "chatModelKey": self._settings.VANE_CHAT_MODEL_KEY or "",
            "embedProviderId": self._settings.VANE_EMBED_PROVIDER_ID or "",
            "embedModelKey": self._settings.VANE_EMBED_MODEL_KEY or "",
        }

        timeout = httpx.Timeout(
            timeout=float(self._settings.VANE_TIMEOUT),
            connect=10.0,
        )

        try:
            response = await self._client.post(
                self._settings.VANE_URL,
                json=body,
                timeout=timeout,
            )
            response.raise_for_status()
            # Vane returns the report as plain text / markdown.
            return response.text
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

        body: dict[str, str] = {
            "message": query,
            "chatProviderId": self._settings.VANE_CHAT_PROVIDER_ID or "",
            "chatModelKey": self._settings.VANE_CHAT_MODEL_KEY or "",
            "embedProviderId": self._settings.VANE_EMBED_PROVIDER_ID or "",
            "embedModelKey": self._settings.VANE_EMBED_MODEL_KEY or "",
        }

        timeout = httpx.Timeout(
            timeout=float(self._settings.VANE_TIMEOUT),
            connect=10.0,
        )

        try:
            async with self._client.stream(
                "POST",
                self._settings.VANE_URL,
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

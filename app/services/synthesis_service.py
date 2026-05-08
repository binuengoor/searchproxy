"""LLM synthesis service — builds prompts and calls LiteLLM chat for /v1/retrieve.

Takes fetched source content and a query, produces a synthesized answer
with inline [N] citations.
"""

from __future__ import annotations

import json
import logging

import httpx

from app.config import Settings
from app.schemas import Citation, SourceChunk

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a research assistant. Given a user query and numbered source excerpts,
produce a concise, factual answer that cites sources inline using [1], [2], etc.

Rules:
- Cite every factual claim with a source number.
- If sources contradict, note the disagreement.
- If no source supports a claim, say so explicitly.
- Keep the answer focused and under 400 words.
- Do not hallucinate or fabricate information.
- Use the same language as the user's query.
"""


def _build_user_content(query: str, sources: list[SourceChunk]) -> str:
    """Build the user message with numbered source excerpts."""
    parts = [f"Query: {query}\n\nSources:\n"]
    for i, src in enumerate(sources, start=1):
        title_line = f"  Title: {src.title}\n" if src.title else ""
        parts.append(f"[{i}] URL: {src.url}\n{title_line}  Content:\n{src.content}\n")
    return "\n".join(parts)


class SynthesisService:
    """Calls LiteLLM chat completions to synthesize an answer from sources."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def synthesize(
        self,
        query: str,
        sources: list[SourceChunk],
    ) -> tuple[str, list[Citation]]:
        """Synthesize an answer from fetched sources.

        Args:
            query: The original research query.
            sources: Fetched source chunks with content.

        Returns:
            (answer, citations) tuple. If synthesis fails, returns
            a fallback message and empty citations.
        """
        if not sources:
            return "No sources were available to synthesize an answer.", []

        user_content = _build_user_content(query, sources)

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._settings.LITELLM_API_KEY:
            headers["Authorization"] = f"Bearer {self._settings.LITELLM_API_KEY}"

        payload = {
            "model": self._settings.LITELLM_CHAT_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
            "max_tokens": 2048,
        }

        log.info(
            "Synthesizing answer for query='%s' with %d sources (model=%s)",
            query,
            len(sources),
            self._settings.LITELLM_CHAT_MODEL,
        )

        try:
            response = await self._client.post(
                self._settings.LITELLM_CHAT_URL,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(30.0, connect=5.0),
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            log.warning("Synthesis timed out for query '%s'", query)
            return _fallback_answer(sources), _extract_citations(sources)
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Synthesis returned HTTP %d for query '%s'",
                exc.response.status_code,
                query,
            )
            return _fallback_answer(sources), _extract_citations(sources)
        except Exception as exc:
            log.warning("Synthesis failed for query '%s': %s", query, exc)
            return _fallback_answer(sources), _extract_citations(sources)

        # Parse OpenAI-compatible response
        try:
            answer = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            log.warning("Synthesis returned unexpected response shape: %s", json.dumps(data)[:500])
            return _fallback_answer(sources), _extract_citations(sources)

        citations = _extract_citations(sources)
        log.info("Synthesized answer for query='%s' (%d chars, %d citations)", query, len(answer), len(citations))
        return answer, citations


def _extract_citations(sources: list[SourceChunk]) -> list[Citation]:
    """Build citation list from sources."""
    return [
        Citation(id=i, url=src.url, title=src.title)
        for i, src in enumerate(sources, start=1)
    ]


def _fallback_answer(sources: list[SourceChunk]) -> str:
    """When synthesis fails, concatenate source excerpts."""
    parts = ["Synthesis unavailable. Showing raw source excerpts:\n"]
    for i, src in enumerate(sources, start=1):
        parts.append(f"[{i}] {src.url}\n{src.content[:500]}...")
    return "\n\n".join(parts)
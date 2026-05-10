"""LLM synthesis service — builds prompts and calls LiteLLM chat for /v1/retrieve.

Takes fetched source content and a query, produces a synthesized answer
with inline [N] citations.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

from app.config import Settings
from app.schemas import Citation, SourceChunk

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a precise research assistant. Your job is to read the numbered source excerpts provided and produce a factual, well-cited answer to the user's query.

## Output structure
1. **Lead answer** — A concise direct answer to the query (2–5 sentences for simple facts; 1–3 short paragraphs for complex or multi-faceted queries).
2. **Key findings** — A bulleted list of the most important supporting points, each with its citation(s). Group related points.
3. **Coverage note** (only when needed) — If the sources do not fully answer the query, say exactly what is missing: "The sources do not address [X]."

## Citation rules
- Every factual claim must have an inline citation [N].
- If a claim is supported by multiple sources, cite all of them: [1][3].
- If sources contradict each other, state both positions and their citations: "Source [1] claims X, whereas Source [2] states Y."
- If no source supports a claim, do not include it. Say "No source supports this" if the user explicitly asked for something absent.

## Quality rules
- Do not hallucinate, infer, or fabricate. Stick to what the sources say.
- Do not copy large blocks of source text. Paraphrase and synthesize.
- Match the user's query language in your answer.
- If the sources are obviously stale, low-quality, or paywalled, note that limitation.
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

    def _build_payload(self, query: str, sources: list[SourceChunk], stream: bool = False) -> dict:
        """Build the LiteLLM chat payload (shared by sync and streaming paths)."""
        user_content = _build_user_content(query, sources)
        return {
            "model": self._settings.LITELLM_CHAT_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
            "max_tokens": self._settings.SYNTHESIS_MAX_TOKENS,
            "stream": stream,
        }

    async def synthesize(
        self,
        query: str,
        sources: list[SourceChunk],
    ) -> tuple[str, list[Citation]]:
        """Synthesize an answer from fetched sources (non-streaming).

        Args:
            query: The original research query.
            sources: Fetched source chunks with content.

        Returns:
            (answer, citations) tuple. If synthesis fails, returns
            a fallback message and empty citations.
        """
        if not sources:
            return "No sources were available to synthesize an answer.", []

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._settings.LITELLM_API_KEY:
            headers["Authorization"] = f"Bearer {self._settings.LITELLM_API_KEY}"

        payload = self._build_payload(query, sources, stream=False)

        total_chars = sum(len(s.content) for s in sources)
        log.info(
            "Synthesizing answer for query='%s' with %d sources, %d total chars (model=%s, max_tokens=%d)",
            query,
            len(sources),
            total_chars,
            self._settings.LITELLM_CHAT_MODEL,
            self._settings.SYNTHESIS_MAX_TOKENS,
        )

        try:
            response = await self._client.post(
                self._settings.LITELLM_CHAT_URL,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(60.0, connect=10.0),
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

    async def synthesize_stream(
        self,
        query: str,
        sources: list[SourceChunk],
    ) -> AsyncIterator[str]:
        """Stream synthesized answer tokens from LiteLLM (SSE format).

        Yields raw content tokens as they arrive from the LLM. The caller
        wraps these in SSE format.

        On failure, yields a single fallback message (no tokens).
        """
        if not sources:
            yield "No sources were available to synthesize an answer."
            return

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._settings.LITELLM_API_KEY:
            headers["Authorization"] = f"Bearer {self._settings.LITELLM_API_KEY}"

        payload = self._build_payload(query, sources, stream=True)

        log.info(
            "Streaming synthesis for query='%s' with %d sources (model=%s, max_tokens=%d)",
            query,
            len(sources),
            self._settings.LITELLM_CHAT_MODEL,
            self._settings.SYNTHESIS_MAX_TOKENS,
        )

        try:
            async with self._client.stream(
                "POST",
                self._settings.LITELLM_CHAT_URL,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(60.0, connect=10.0),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    # OpenAI streaming shape: choices[0].delta.content
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        yield token
        except httpx.TimeoutException:
            log.warning("Streaming synthesis timed out for query '%s'", query)
            yield _fallback_answer(sources)
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Streaming synthesis returned HTTP %d for query '%s'",
                exc.response.status_code,
                query,
            )
            yield _fallback_answer(sources)
        except Exception as exc:
            log.warning("Streaming synthesis failed for query '%s': %s", query, exc)
            yield _fallback_answer(sources)


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
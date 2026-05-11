"""Shared schemas for OpenAPI documentation.

Models defined here have no runtime function — they exist purely to give
Pydantic+FastAPI enough shape to emit rich OpenAPI fields (typed arrays,
field-level descriptions, examples) that Pydantic v1/v2 otherwise collapse
when given ``dict[str, Any]``.

Import into request-body models or response-body models as needed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MessageItem(BaseModel):
    """One entry in an OpenAI-style ``messages`` array.

    Only the fields needed for query extraction are typed here.
    Extra keys (``name``, ``tool_calls``, etc.) are silently accepted.
    """

    role: str = Field(..., description="Message role: ``user``, ``assistant``, or ``system``.")
    content: str | None = Field(default="", description="Message text content. Null for assistant tool-call messages.")


# ---------------------------------------------------------------------------
# /v1/retrieve schemas
# ---------------------------------------------------------------------------

class Citation(BaseModel):
    """A numbered source citation within a synthesized answer."""

    id: int = Field(..., description="Citation number referenced in the answer text, e.g. 1 for [1].")
    url: str = Field(..., description="Source URL.")
    title: str = Field(default="", description="Page title or snippet header.")
    relevance_score: float | None = Field(
        default=None,
        description="BGE reranker relevance score for this source (0–1). Higher = more relevant.",
    )


class RetrieveRequest(BaseModel):
    """Request body for ``POST /v1/retrieve``.

    Sends a query, gets back a synthesized answer with inline citations
    fetched from the top search results.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "query": "Apple M3 chip announcement date",
                    "max_results": 10,
                    "fetch_top_k": 5,
                    "synthesize": True,
                    "stream": False,
                }
            ]
        }
    )

    query: str = Field(
        ...,
        min_length=1,
        description="Research query. The pipeline searches, reranks, fetches, and synthesizes an answer.",
    )
    max_results: int = Field(
        default=10, ge=1, le=50,
        description="Number of search results to retrieve from LiteLLM before reranking.",
    )
    fetch_top_k: int = Field(
        default=5, ge=1, le=10,
        description="Number of top-ranked results to fetch content from after reranking.",
    )
    synthesize: bool = Field(
        default=True,
        description="If false, return fetched sources without LLM synthesis (raw markdown chunks).",
    )
    stream: bool = Field(
        default=False,
        description="If true, return an SSE stream with source metadata arriving progressively as fetches complete, followed by real-time LLM synthesis tokens. Requires synthesize=true.",
    )


class SourceChunk(BaseModel):
    """A fetched source with its content chunked for synthesis."""

    url: str = Field(..., description="Source URL.")
    title: str = Field(default="", description="Page title.")
    content: str = Field(default="", description="Chunked content (up to max_content_per_source chars).")
    fetch_tier: str | None = Field(
        default=None,
        description="Which fetch tier produced this content: crawl4ai, jina, scrape_do, scraperapi.",
    )
    content_length: int | None = Field(
        default=None,
        description="Length of fetched content in characters (post-cleaning, pre-truncation).",
    )
    relevance_score: float | None = Field(
        default=None,
        description="BGE reranker relevance score for this source (0–1). Higher = more relevant.",
    )
    fetch_time_ms: float | None = Field(
        default=None,
        description="Time spent fetching this source in milliseconds.",
    )


class RetrieveResponse(BaseModel):
    """Response from ``POST /v1/retrieve``."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "query": "Apple M3 chip announcement date",
                    "answer": "Apple announced the M3 chip in October 2023 [1]. The M3 family includes M3, M3 Pro, and M3 Max [2].",
                    "citations": [
                        {"id": 1, "url": "https://apple.com/newsroom/...", "title": "Apple Unveils M3", "relevance_score": 0.95},
                        {"id": 2, "url": "https://theverge.com/...", "title": "Apple M3 Review", "relevance_score": 0.87},
                    ],
                    "sources": [
                        {
                            "url": "https://apple.com/newsroom/...",
                            "title": "Apple Unveils M3",
                            "content": "Apple today announced M3...",
                            "fetch_tier": "crawl4ai",
                            "content_length": 4200,
                            "relevance_score": 0.95,
                            "fetch_time_ms": 1240.5,
                        }
                    ],
                    "sources_fetched": 5,
                    "sources_failed": 0,
                }
            ]
        }
    )

    query: str = Field(..., description="The original query.")
    answer: str = Field(default="", description="Synthesized answer with inline [N] citations. Empty if synthesize=false.")
    citations: list[Citation] = Field(default_factory=list, description="Ordered list of cited sources.")
    sources: list[SourceChunk] = Field(
        default_factory=list,
        description="Raw fetched source chunks. Populated when synthesize=false or for debugging.",
    )
    sources_fetched: int = Field(default=0, description="Number of sources successfully fetched.")
    sources_failed: int = Field(default=0, description="Number of sources that failed to fetch.")

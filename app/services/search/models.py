"""Data models for search results and provider management."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class SearchResult(BaseModel):
    """A single normalized search result across all providers."""

    title: str = Field(..., description="Page title.")
    url: str = Field(..., description="Source URL.")
    snippet: str = Field(..., description="Short content summary (excerpt) from the page.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "title": "Real Madrid CF - Wikipedia",
                    "url": "https://en.wikipedia.org/wiki/Real_Madrid_CF",
                    "snippet": "Real Madrid Club de Fútbol, commonly referred to as Real Madrid...",
                }
            ]
        }
    )


class SearchResponse(BaseModel):
    """Search results wrapper matching standard search shape."""

    results: list[SearchResult] = Field(
        default_factory=list,
        description="List of search results; may be empty on error or no matches.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "results": [
                        {
                            "title": "Real Madrid CF - Wikipedia",
                            "url": "https://en.wikipedia.org/wiki/Real_Madrid_CF",
                            "snippet": "Real Madrid Club de Fútbol, commonly referred to as Real Madrid...",
                        }
                    ]
                }
            ]
        }
    )


class ProviderStatus(BaseModel):
    """Health and metric tracking for a single search provider."""

    name: str
    tier: int = 1
    is_available: bool = True
    cooldown_until: float = 0.0
    total_requests: int = 0
    failed_requests: int = 0
    last_error: str | None = None
    extra_metadata: dict[str, Any] = Field(default_factory=dict)

"""Shared result models for the fetch chain.

Lives here instead of in crawl4ai.py because FetchResult is the universal
contract across ALL fetch tiers (Crawl4AI, Jina Reader, anti-bot services)
and the fetch chain orchestrator. No service should depend on a peer service
for its result type.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FetchResult(BaseModel):
    """Result of a fetch operation across all tiers."""

    success: bool = Field(default=False)
    url: str = Field(default="")
    markdown: str = Field(default="")
    title: str = Field(default="")
    description: str = Field(default="")
    language: str = Field(default="")
    error: str = Field(default="")
    status_code: int | None = Field(default=None)
    source: str = Field(default="")  # which tier succeeded: "crawl4ai", "jina", "scrape_do", "scraperapi"
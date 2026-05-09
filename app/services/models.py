"""Shared result models for the fetch chain.

Lives here instead of in crawl4ai.py because FetchResult is the universal
contract across ALL fetch tiers (Crawl4AI, Jina Reader, anti-bot services)
and the fetch chain orchestrator. No service should depend on a peer service
for its result type.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FetchResult(BaseModel):
    """Result of a fetch operation across all tiers."""

    success: bool = Field(default=False, description="Whether the fetch succeeded and returned usable content.")
    url: str = Field(default="", description="The original requested URL.")
    markdown: str = Field(default="", description="Extracted content in Markdown format. Empty if success=false.")
    title: str = Field(default="", description="Page title, if extractable.")
    description: str = Field(default="", description="Meta description or summary.")
    language: str = Field(default="", description="Detected language code (e.g. 'en'), if available.")
    error: str = Field(default="", description="Human-readable error message when success=false.")
    status_code: int | None = Field(default=None, description="HTTP status code from the successful tier, if any.")
    source: str = Field(default="", description="Which tier produced the result: crawl4ai, jina, scrape_do, scraperapi, or empty.")
    fetch_time_ms: float | None = Field(default=None, description="Time spent fetching this URL in milliseconds (entire tier chain).")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "success": True,
                    "url": "https://en.wikipedia.org/wiki/Real_Madrid_CF",
                    "markdown": "# Real Madrid CF\n\nReal Madrid Club de Fútbol...",
                    "title": "Real Madrid CF - Wikipedia",
                    "description": "Real Madrid Club de Fútbol, commonly referred to as Real Madrid...",
                    "language": "en",
                    "error": "",
                    "status_code": 200,
                    "source": "crawl4ai",
                    "fetch_time_ms": 1240.5,
                },
                {
                    "success": False,
                    "url": "https://example.com/blocked",
                    "markdown": "",
                    "title": "",
                    "description": "",
                    "language": "",
                    "error": "All fetch tiers exhausted; page appears blocked.",
                    "status_code": None,
                    "source": "",
                    "fetch_time_ms": 45200.0,
                },
            ]
        }
    )

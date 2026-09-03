"""Tests for Tier 0 FastFetch client and FetchChain integration."""

from __future__ import annotations

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.config import Settings
from app.services.fast_fetch_client import FastFetchClient
from app.services.fetch_chain import FetchChain
from app.services.models import FetchResult


@pytest.fixture
def fast_settings():
    return Settings(
        FAST_FETCH_ENABLED=True,
        FAST_FETCH_TIMEOUT=3.0,
        RETRIEVE_MIN_CONTENT_LENGTH=50,
    )


@pytest.mark.asyncio
async def test_fast_fetch_success(fast_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    html = """
    <html>
        <head><title>Test Article</title></head>
        <body>
            <article>
                <h1>Test Article Heading</h1>
                <p>This is a long enough paragraph with substantial content for testing FastFetch Trafilatura extraction. It contains multiple sentences to exceed the minimum threshold easily.</p>
            </article>
        </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = html
    client.get = AsyncMock(return_value=mock_resp)

    fetcher = FastFetchClient(client=client, settings=fast_settings)
    res = await fetcher.fetch("https://example.com/article")

    assert res.success is True
    assert res.source == "http_fast"
    assert res.title == "Test Article"
    assert "substantial content" in res.markdown
    assert res.fetch_time_ms is not None


@pytest.mark.asyncio
async def test_fast_fetch_anti_bot_detection(fast_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.text = "<html><body>Cloudflare Just a moment... Please verify you are human</body></html>"
    client.get = AsyncMock(return_value=mock_resp)

    fetcher = FastFetchClient(client=client, settings=fast_settings)
    res = await fetcher.fetch("https://example.com/protected")

    assert res.success is False
    assert "Anti-bot" in res.error


@pytest.mark.asyncio
async def test_fast_fetch_too_short_spa_fallthrough(fast_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><body><div id='root'></div><script src='app.js'></script></body></html>"
    client.get = AsyncMock(return_value=mock_resp)

    fetcher = FastFetchClient(client=client, settings=fast_settings)
    res = await fetcher.fetch("https://example.com/spa")

    assert res.success is False
    assert "requires JavaScript" in res.error


@pytest.mark.asyncio
async def test_fetch_chain_skips_crawl4ai_when_fast_fetch_succeeds(fast_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    chain = FetchChain(client=client, settings=fast_settings)

    # Mock fast fetch success
    chain._fast_fetch.fetch = AsyncMock(return_value=FetchResult(
        success=True,
        url="https://example.com/fast",
        markdown="# Fast Content",
        title="Fast Title",
        source="http_fast",
    ))
    chain._crawl4ai.fetch_markdown = AsyncMock()

    result = await chain.execute("https://example.com/fast")
    assert result.success is True
    assert result.source == "http_fast"
    assert result.markdown == "# Fast Content"
    chain._crawl4ai.fetch_markdown.assert_not_awaited()

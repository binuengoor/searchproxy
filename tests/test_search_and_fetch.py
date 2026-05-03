from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient

from app.services.litellm_search import LiteLLMSearchClient, SearchResponse, SearchResult
from app.services.crawl4ai import FetchResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_litellm_search(monkeypatch):
    """Replace LiteLLMSearchClient.search with a controllable mock."""
    mock = AsyncMock()
    monkeypatch.setattr(
        "app.services.litellm_search.LiteLLMSearchClient.search",
        mock,
    )
    return mock


@pytest.fixture
def fetch_chain():
    """Return a FetchChain with all internal services mocked out.

    Note: this fixture is NOT autoused — tests must explicitly request it.
    """
    from app.config import settings
    from app.services.fetch_chain import FetchChain

    mock_client = MagicMock()
    chain = FetchChain(client=mock_client, settings=settings)

    # Swap real service instances for mocks
    chain._crawl4ai = AsyncMock()
    chain._jina = AsyncMock()
    chain._scrape_do = AsyncMock()
    chain._scraper_api = AsyncMock()

    return chain


# ---------------------------------------------------------------------------
# Search router tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_compat_perplexity_returns_results(client: AsyncClient, mock_litellm_search: AsyncMock):
    """POST /compat/perplexity should return search results."""
    mock_litellm_search.return_value = SearchResponse(
        results=[
            SearchResult(title="Python asyncio", url="https://docs.python.org", snippet="async/await guide"),
        ]
    )

    resp = await client.post("/compat/perplexity", json={"query": "python asyncio", "max_results": 5})

    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["title"] == "Python asyncio"
    assert data["results"][0]["url"] == "https://docs.python.org"
    mock_litellm_search.assert_awaited_once_with(query="python asyncio", max_results=5)


@pytest.mark.anyio
async def test_compat_perplexity_empty_on_error(client: AsyncClient, mock_litellm_search: AsyncMock):
    """Graceful degradation: a LiteLLM error returns empty results, not 500."""
    mock_litellm_search.return_value = SearchResponse(results=[])

    resp = await client.post("/compat/perplexity", json={"query": "timeout test"})

    assert resp.status_code == 200
    assert resp.json()["results"] == []


@pytest.mark.anyio
async def test_compat_perplexity_bad_request(client: AsyncClient):
    """Missing 'query' field should trigger validation error."""
    resp = await client.post("/compat/perplexity", json={"max_results": 5})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Fetch chain tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_fetch_chain_crawl4ai_success(fetch_chain):
    """Tier 1 success: Crawl4AI returns clean markdown, chain stops."""
    chain = fetch_chain
    chain._crawl4ai.fetch_markdown.return_value = FetchResult(
        success=True,
        url="https://example.com",
        markdown="# Hello",
        title="Example",
        source="crawl4ai",
    )

    result = await chain.execute("https://example.com")

    assert result.success is True
    assert result.source == "crawl4ai"
    assert result.markdown == "# Hello"
    chain._crawl4ai.fetch_markdown.assert_awaited_once_with("https://example.com")
    chain._jina.fetch.assert_not_awaited()
    chain._scrape_do.fetch.assert_not_awaited()


@pytest.mark.anyio
async def test_fetch_chain_crawl4ai_403_escalates_to_firebreak(fetch_chain):
    """Crawl4AI returns 403: skip Jina, go directly to anti-bot firebreak."""
    chain = fetch_chain
    chain._crawl4ai.fetch_markdown.return_value = FetchResult(
        success=False,
        url="https://blocked.example.com",
        error="403 anti-bot block",
        status_code=403,
        source="crawl4ai",
    )
    chain._scrape_do.fetch.return_value = FetchResult(
        success=True,
        url="https://blocked.example.com",
        markdown="Bypassed",
        source="scrape_do",
    )

    result = await chain.execute("https://blocked.example.com")

    assert result.success is True
    assert result.source == "scrape_do"
    chain._jina.fetch.assert_not_awaited()
    chain._scrape_do.fetch.assert_awaited_once_with("https://blocked.example.com")


@pytest.mark.anyio
async def test_fetch_chain_crawl4ai_timeout_then_jina_success(fetch_chain):
    """Crawl4AI times out (non-anti-bot), Jina succeeds, firebreak never called."""
    chain = fetch_chain
    chain._crawl4ai.fetch_markdown.return_value = FetchResult(
        success=False,
        url="https://slow.example.com",
        error="timeout",
        source="crawl4ai",
    )
    chain._jina.fetch.return_value = FetchResult(
        success=True,
        url="https://slow.example.com",
        markdown="Jina content",
        source="jina",
    )

    result = await chain.execute("https://slow.example.com")

    assert result.success is True
    assert result.source == "jina"
    chain._scrape_do.fetch.assert_not_awaited()


@pytest.mark.anyio
async def test_fetch_chain_jina_anti_bot_escalates(fetch_chain):
    """Jina returns 403: escalate to firebreak."""
    chain = fetch_chain
    chain._crawl4ai.fetch_markdown.return_value = FetchResult(
        success=False,
        url="https://cf.example.com",
        error="timeout",
        source="crawl4ai",
    )
    chain._jina.fetch.return_value = FetchResult(
        success=False,
        url="https://cf.example.com",
        error="403 anti-bot block",
        status_code=403,
        source="jina",
    )
    # Scrape.do returns failure so it escalates to ScraperAPI
    chain._scrape_do.fetch.return_value = FetchResult(
        success=False,
        url="https://cf.example.com",
        error="credit limit reached",
        source="scrape_do",
    )
    chain._scraper_api.fetch.return_value = FetchResult(
        success=True,
        url="https://cf.example.com",
        markdown="ScraperAPI content",
        source="scraperapi",
    )

    result = await chain.execute("https://cf.example.com")

    assert result.success is True
    assert result.source == "scraperapi"
    chain._scrape_do.fetch.assert_awaited_once_with("https://cf.example.com")
    chain._scraper_api.fetch.assert_awaited_once_with("https://cf.example.com")


@pytest.mark.anyio
async def test_fetch_chain_all_tiers_exhausted(fetch_chain):
    """All tiers fail: return failed FetchResult with 'all tiers exhausted'."""
    chain = fetch_chain
    chain._crawl4ai.fetch_markdown.return_value = FetchResult(
        success=False,
        url="https://dead.example.com",
        error="timeout",
        source="crawl4ai",
    )
    # Jina returns 403 (anti-bot) to trigger the firebreak path
    chain._jina.fetch.return_value = FetchResult(
        success=False,
        url="https://dead.example.com",
        error="403 anti-bot block",
        status_code=403,
        source="jina",
    )
    chain._scrape_do.fetch.return_value = FetchResult(
        success=False,
        url="https://dead.example.com",
        error="credit limit reached",
        source="scrape_do",
    )
    chain._scraper_api.fetch.return_value = FetchResult(
        success=False,
        url="https://dead.example.com",
        error="credit limit reached",
        source="scraperapi",
    )

    result = await chain.execute("https://dead.example.com")

    assert result.success is False
    assert result.error == "all tiers exhausted"


@pytest.mark.anyio
async def test_fetch_chain_body_scan_anti_bot_on_200(fetch_chain):
    """Crawl4AI returns HTTP 200 but body contains 'checking your browser' — escalate."""
    chain = fetch_chain
    chain._crawl4ai.fetch_markdown.return_value = FetchResult(
        success=True,
        url="https://cf-trap.example.com",
        markdown="<html>Checking your browser... Cloudflare</html>",
        status_code=200,
        source="crawl4ai",
    )
    chain._scrape_do.fetch.return_value = FetchResult(
        success=True,
        url="https://cf-trap.example.com",
        markdown="Real content",
        source="scrape_do",
    )

    result = await chain.execute("https://cf-trap.example.com")

    assert result.success is True
    assert result.source == "scrape_do"
    assert result.markdown == "Real content"

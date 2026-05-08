"""Tests for /v1/retrieve — search → rerank → fetch → synthesize pipeline.

All upstream calls (LiteLLM search, cf-inference rerank, fetch chain,
LiteLLM chat) are mocked so tests run without network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.schemas import Citation
from app.services.litellm_search import SearchResponse, SearchResult
from app.services.rerank_service import RerankResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockFetchResult:
    """Mimics app.services.models.FetchResult."""

    def __init__(self, success: bool, url: str = "", markdown: str = "",
                 title: str = "", error: str = "", status_code: int | None = 200,
                 source: str = "crawl4ai"):
        self.success = success
        self.url = url
        self.markdown = markdown
        self.title = title
        self.error = error
        self.status_code = status_code
        self.source = source


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_search(monkeypatch):
    """Replace LiteLLMSearchClient.search with a controllable mock."""
    mock = AsyncMock()
    monkeypatch.setattr("app.services.litellm_search.LiteLLMSearchClient.search", mock)
    return mock


@pytest.fixture
def mock_rerank(monkeypatch):
    """Replace RerankService.rerank with a controllable mock."""
    mock = AsyncMock()
    monkeypatch.setattr("app.services.rerank_service.RerankService.rerank", mock)
    return mock


@pytest.fixture
def mock_fetch(monkeypatch):
    """Replace FetchChain.execute with a controllable mock."""
    mock = AsyncMock()
    monkeypatch.setattr("app.services.fetch_chain.FetchChain.execute", mock)
    return mock


@pytest.fixture
def mock_synthesize(monkeypatch):
    """Replace SynthesisService.synthesize with a controllable mock."""
    mock = AsyncMock()
    monkeypatch.setattr("app.services.synthesis_service.SynthesisService.synthesize", mock)
    return mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_retrieve_success_flow(
    client: AsyncClient,
    mock_search: AsyncMock,
    mock_rerank: AsyncMock,
    mock_fetch: AsyncMock,
    mock_synthesize: AsyncMock,
):
    """Full pipeline: search → rerank → fetch → synthesize."""
    mock_search.return_value = SearchResponse(results=[
        SearchResult(title="Apple M3 Announcement", url="https://apple.com/m3", snippet="Apple announced the M3 chip."),
        SearchResult(title="M3 Chip Review", url="https://theverge.com/m3", snippet="The M3 features 3nm."),
        SearchResult(title="M3 vs M2", url="https://arstechnica.com/m3", snippet="GPU improvements."),
    ])
    mock_rerank.return_value = [
        RerankResult(index=0, relevance_score=0.95, text="Apple M3 Announcement"),
        RerankResult(index=1, relevance_score=0.87, text="M3 Chip Review"),
    ]
    mock_fetch.return_value = MockFetchResult(
        success=True, url="https://apple.com/m3",
        markdown="Apple announced the M3 chip in October 2023.",
        title="Apple M3 Announcement",
    )
    mock_synthesize.return_value = (
        "Apple announced the M3 chip in October 2023 [1]. The M3 features a 3nm process [2].",
        [
            Citation(id=1, url="https://apple.com/m3", title="Apple M3 Announcement"),
            Citation(id=2, url="https://theverge.com/m3", title="M3 Chip Review"),
        ],
    )

    resp = await client.post("/v1/retrieve", json={"query": "Apple M3 chip"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "Apple M3 chip"
    assert len(data["citations"]) >= 1
    assert data["sources_fetched"] >= 1


@pytest.mark.anyio
async def test_retrieve_no_search_results(client: AsyncClient, mock_search: AsyncMock):
    """When search returns 0 results, response has empty answer/citations."""
    mock_search.return_value = SearchResponse(results=[])

    resp = await client.post("/v1/retrieve", json={"query": "obscure query"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "obscure query"
    assert data["answer"] == ""
    assert data["sources_fetched"] == 0
    assert data["sources_failed"] == 0


@pytest.mark.anyio
async def test_retrieve_rerank_failure_fallback(
    client: AsyncClient,
    mock_search: AsyncMock,
    mock_rerank: AsyncMock,
    mock_fetch: AsyncMock,
    mock_synthesize: AsyncMock,
):
    """When rerank fails, pipeline falls back to original search order."""
    mock_search.return_value = SearchResponse(results=[
        SearchResult(title="Test", url="https://example.com", snippet="Test snippet"),
    ])
    mock_rerank.return_value = None  # Rerank fails
    mock_fetch.return_value = MockFetchResult(
        success=True, url="https://example.com",
        markdown="Test content", title="Test",
    )
    mock_synthesize.return_value = (
        "Test answer.",
        [Citation(id=1, url="https://example.com", title="Test")],
    )

    resp = await client.post("/v1/retrieve", json={"query": "test query"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["sources_fetched"] == 1


@pytest.mark.anyio
async def test_retrieve_fetch_failure(
    client: AsyncClient,
    mock_search: AsyncMock,
    mock_rerank: AsyncMock,
    mock_fetch: AsyncMock,
):
    """When all fetches fail, response includes failure message."""
    mock_search.return_value = SearchResponse(results=[
        SearchResult(title="Test", url="https://example.com", snippet="Test snippet"),
    ])
    mock_rerank.return_value = [
        RerankResult(index=0, relevance_score=0.9, text="Test"),
    ]
    mock_fetch.return_value = MockFetchResult(
        success=False, url="https://example.com", error="all tiers exhausted",
    )

    resp = await client.post("/v1/retrieve", json={"query": "test query"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["sources_failed"] == 1
    assert data["sources_fetched"] == 0


@pytest.mark.anyio
async def test_retrieve_no_synthesize(
    client: AsyncClient,
    mock_search: AsyncMock,
    mock_rerank: AsyncMock,
    mock_fetch: AsyncMock,
):
    """When synthesize=false, return raw source chunks without LLM call."""
    mock_search.return_value = SearchResponse(results=[
        SearchResult(title="Test", url="https://example.com", snippet="Test snippet"),
    ])
    mock_rerank.return_value = [
        RerankResult(index=0, relevance_score=0.9, text="Test"),
    ]
    mock_fetch.return_value = MockFetchResult(
        success=True, url="https://example.com",
        markdown="Full content here", title="Test",
    )

    resp = await client.post("/v1/retrieve", json={"query": "test", "synthesize": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == ""  # No synthesis
    assert len(data["sources"]) >= 1
    assert data["sources"][0]["content"] == "Full content here"


@pytest.mark.anyio
async def test_retrieve_missing_query(client: AsyncClient):
    """Missing query should return 422 validation error."""
    resp = await client.post("/v1/retrieve", json={})
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_retrieve_dedup_by_canonical_url():
    """Duplicate URLs (different scheme/www) should be deduplicated."""
    from app.services.retrieve_service import _canonical_key

    assert _canonical_key("https://apple.com/m3") == _canonical_key("http://www.apple.com/m3")
    assert _canonical_key("https://example.com/path/") == _canonical_key("https://example.com/path")


@pytest.mark.anyio
async def test_retrieve_partial_fetch_failure(
    client: AsyncClient,
    mock_search: AsyncMock,
    mock_rerank: AsyncMock,
    mock_fetch: AsyncMock,
    mock_synthesize: AsyncMock,
):
    """When some fetches succeed and some fail, response reflects both counts."""
    call_count = 0

    async def side_effect_fetch(url: str):
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return MockFetchResult(
                success=True, url=url, markdown="Good content", title="OK",
            )
        return MockFetchResult(
            success=False, url=url, error="failed",
        )

    mock_fetch.side_effect = side_effect_fetch
    mock_search.return_value = SearchResponse(results=[
        SearchResult(title="A", url="https://a.com", snippet="a"),
        SearchResult(title="B", url="https://b.com", snippet="b"),
    ])
    mock_rerank.return_value = [
        RerankResult(index=0, relevance_score=0.9, text="A"),
        RerankResult(index=1, relevance_score=0.7, text="B"),
    ]
    mock_synthesize.return_value = (
        "Answer [1].",
        [Citation(id=1, url="https://a.com", title="A")],
    )

    resp = await client.post("/v1/retrieve", json={"query": "test partial", "fetch_top_k": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["sources_fetched"] >= 1
    assert data["sources_failed"] >= 1
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
                 source: str = "crawl4ai", fetch_time_ms: float | None = None):
        self.success = success
        self.url = url
        self.markdown = markdown
        self.title = title
        self.error = error
        self.status_code = status_code
        self.source = source
        self.fetch_time_ms = fetch_time_ms


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


@pytest.fixture
def mock_synthesize_stream(monkeypatch):
    """Replace SynthesisService.synthesize_stream with a controllable mock."""
    async def _stream(*args, **kwargs):
        yield "Token1 "
        yield "Token2"
    monkeypatch.setattr("app.services.synthesis_service.SynthesisService.synthesize_stream", _stream)
    return _stream


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
        markdown="Apple announced the M3 chip in October 2023 during their Scary Fast event. The M3 family includes three chips: the base M3, M3 Pro, and M3 Max. These chips are built on a 3-nanometer process and represent a significant leap in performance and efficiency for Apple Silicon. The M3 chip features an 8-core CPU and a 10-core GPU, making it ideal for everyday pro workflows.",
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
    # Metadata enrichment assertions
    if data["sources"]:
        src = data["sources"][0]
        assert "fetch_tier" in src
        assert "content_length" in src
        assert "relevance_score" in src
        assert "fetch_time_ms" in src
    if data["citations"]:
        assert "relevance_score" in data["citations"][0]


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
        markdown="This is a comprehensive test article with substantial content that discusses the topic in detail. It includes multiple paragraphs covering various aspects of the subject matter, providing enough depth and breadth to exceed the minimum content threshold required by the quality gates. The article goes on to explain key concepts with sufficient detail to be useful for synthesis.", title="Test",
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
        markdown="This is a comprehensive article with full content covering the topic in extensive detail. The article includes multiple sections with deep analysis, examples, and supporting information that provides substantial value for synthesis. It continues with additional paragraphs explaining the nuances and implications of the subject matter in depth.", title="Test",
    )

    resp = await client.post("/v1/retrieve", json={"query": "test", "synthesize": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == ""  # No synthesis
    assert len(data["sources"]) >= 1
    assert "comprehensive article with full content" in data["sources"][0]["content"]


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
    async def side_effect_fetch(url: str, aggressive_clean: bool = False):
        if "a.com" in url:
            return MockFetchResult(
                success=True, url=url, markdown="This is a comprehensive article with good content that covers the topic in substantial detail. The article provides multiple paragraphs of valuable information including analysis, examples, and supporting data that makes it highly suitable for synthesis and produces a high quality research result. Additional sections provide more depth and context for a complete understanding.", title="OK",
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


@pytest.mark.anyio
async def test_retrieve_skips_too_short_content(
    client: AsyncClient,
    mock_search: AsyncMock,
    mock_rerank: AsyncMock,
    mock_fetch: AsyncMock,
    mock_synthesize: AsyncMock,
):
    """Sources under min length are skipped; synthesis sees only valid ones."""
    mock_search.return_value = SearchResponse(results=[
        SearchResult(title="Short", url="https://short.com", snippet="short"),
        SearchResult(title="Good", url="https://good.com", snippet="good"),
    ])
    mock_rerank.return_value = [
        RerankResult(index=0, relevance_score=0.5, text="Short"),
        RerankResult(index=1, relevance_score=0.9, text="Good"),
    ]

    async def side_effect(url: str, aggressive_clean: bool = False):
        if "short" in url:
            return MockFetchResult(success=True, url=url, markdown="Hi", title="Short")
        return MockFetchResult(success=True, url=url, markdown="This is a comprehensive and detailed article that thoroughly covers the subject matter with extensive analysis, multiple examples, and in-depth discussion across many sections. The content provides substantial value through its comprehensive coverage of key topics, detailed explanations of important concepts, and practical insights that make it an excellent source for synthesis and research purposes. Additional paragraphs explore related themes and provide further context and depth.", title="Good")

    mock_fetch.side_effect = side_effect
    mock_synthesize.return_value = (
        "Good answer [1].",
        [Citation(id=1, url="https://good.com", title="Good")],
    )

    resp = await client.post("/v1/retrieve", json={"query": "test", "fetch_top_k": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["sources_fetched"] == 1
    assert data["sources_failed"] == 0  # fetch succeeded, just filtered by quality gate


@pytest.mark.anyio
async def test_retrieve_skips_paywall_content(
    client: AsyncClient,
    mock_search: AsyncMock,
    mock_rerank: AsyncMock,
    mock_fetch: AsyncMock,
    mock_synthesize: AsyncMock,
):
    """Sources detected as paywall are skipped; synthesis sees only valid ones."""
    mock_search.return_value = SearchResponse(results=[
        SearchResult(title="Paywall", url="https://paywall.com", snippet="paywall"),
        SearchResult(title="Good", url="https://good.com", snippet="good"),
    ])
    mock_rerank.return_value = [
        RerankResult(index=0, relevance_score=0.5, text="Paywall"),
        RerankResult(index=1, relevance_score=0.9, text="Good"),
    ]

    async def side_effect(url: str, aggressive_clean: bool = False):
        if "paywall" in url:
            return MockFetchResult(
                success=True, url=url,
                markdown="Please subscribe to continue reading this premium content. Sign in to view the full article.",
                title="Paywall",
            )
        return MockFetchResult(
            success=True, url=url,
            markdown="This is a comprehensive and detailed article that thoroughly covers the subject matter with extensive analysis, multiple examples, and in-depth discussion across many sections. The content provides substantial value through its comprehensive coverage of key topics, detailed explanations of important concepts, and practical insights that make it an excellent source for synthesis and research purposes.",
            title="Good",
        )

    mock_fetch.side_effect = side_effect
    mock_synthesize.return_value = (
        "Good answer [1].",
        [Citation(id=1, url="https://good.com", title="Good")],
    )

    resp = await client.post("/v1/retrieve", json={"query": "test", "fetch_top_k": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["sources_fetched"] == 1
    assert data["sources_failed"] == 0  # fetch succeeded, just filtered by quality gate


@pytest.mark.anyio
async def test_retrieve_all_sources_filtered_by_quality_gates(
    client: AsyncClient,
    mock_search: AsyncMock,
    mock_rerank: AsyncMock,
    mock_fetch: AsyncMock,
):
    """When all sources are filtered out, return appropriate message."""
    mock_search.return_value = SearchResponse(results=[
        SearchResult(title="Bad", url="https://bad.com", snippet="bad"),
    ])
    mock_rerank.return_value = [
        RerankResult(index=0, relevance_score=0.5, text="Bad"),
    ]
    mock_fetch.return_value = MockFetchResult(
        success=True, url="https://bad.com",
        markdown="Please subscribe to continue.",
        title="Bad",
    )

    resp = await client.post("/v1/retrieve", json={"query": "test", "fetch_top_k": 1})
    assert resp.status_code == 200
    data = resp.json()
    assert data["sources_fetched"] == 0
    assert data["sources_failed"] == 1
    assert "filtered out" in data["answer"].lower() or "paywalled" in data["answer"].lower()


@pytest.mark.anyio
async def test_retrieve_source_metadata_enrichment(
    client: AsyncClient,
    mock_search: AsyncMock,
    mock_rerank: AsyncMock,
    mock_fetch: AsyncMock,
    mock_synthesize: AsyncMock,
):
    """SourceChunk and Citation models carry fetch_tier, content_length, relevance_score, fetch_time_ms."""
    mock_search.return_value = SearchResponse(results=[
        SearchResult(title="Meta", url="https://meta.com", snippet="meta"),
    ])
    mock_rerank.return_value = [
        RerankResult(index=0, relevance_score=0.92, text="Meta"),
    ]
    mock_fetch.return_value = MockFetchResult(
        success=True, url="https://meta.com",
        markdown="This is a comprehensive and detailed article that thoroughly covers the subject matter with extensive analysis, multiple examples, and in-depth discussion across many sections. The content provides substantial value through its comprehensive coverage of key topics, detailed explanations of important concepts, and practical insights that make it an excellent source for synthesis and research purposes. Additional paragraphs explore related themes and provide further context and depth.",
        title="Meta", source="crawl4ai", fetch_time_ms=1240.5,
    )
    mock_synthesize.return_value = (
        "Meta answer [1].",
        [Citation(id=1, url="https://meta.com", title="Meta", relevance_score=0.92)],
    )

    resp = await client.post("/v1/retrieve", json={"query": "meta", "synthesize": True})
    assert resp.status_code == 200
    data = resp.json()

    assert data["sources_fetched"] == 1
    assert len(data["sources"]) == 1
    src = data["sources"][0]
    assert src["fetch_tier"] == "crawl4ai"
    assert src["content_length"] == len(mock_fetch.return_value.markdown)
    assert src["relevance_score"] == 0.92
    assert src["fetch_time_ms"] == 1240.5

    assert len(data["citations"]) == 1
    cit = data["citations"][0]
    assert cit["relevance_score"] == 0.92


@pytest.mark.anyio
async def test_retrieve_no_synthesize_returns_metadata(
    client: AsyncClient,
    mock_search: AsyncMock,
    mock_rerank: AsyncMock,
    mock_fetch: AsyncMock,
):
    """When synthesize=false, sources still include full metadata."""
    mock_search.return_value = SearchResponse(results=[
        SearchResult(title="Raw", url="https://raw.com", snippet="raw"),
    ])
    mock_rerank.return_value = [
        RerankResult(index=0, relevance_score=0.88, text="Raw"),
    ]
    mock_fetch.return_value = MockFetchResult(
        success=True, url="https://raw.com",
        markdown="This is a comprehensive and detailed article that thoroughly covers the subject matter with extensive analysis, multiple examples, and in-depth discussion across many sections. The content provides substantial value through its comprehensive coverage of key topics, detailed explanations of important concepts, and practical insights that make it an excellent source for synthesis and research purposes. Additional paragraphs explore related themes and provide further context and depth.",
        title="Raw", source="jina", fetch_time_ms=850.0,
    )

    resp = await client.post("/v1/retrieve", json={"query": "raw", "synthesize": False})
    assert resp.status_code == 200
    data = resp.json()

    assert data["answer"] == ""
    assert len(data["sources"]) == 1
    src = data["sources"][0]
    assert src["fetch_tier"] == "jina"
    assert src["content_length"] == len(mock_fetch.return_value.markdown)
    assert src["relevance_score"] == 0.88
    assert src["fetch_time_ms"] == 850.0

    assert len(data["citations"]) == 1
    cit = data["citations"][0]
    assert cit["relevance_score"] == 0.88


# ---------------------------------------------------------------------------
# Streaming tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_retrieve_streaming_success(
    client: AsyncClient,
    mock_search: AsyncMock,
    mock_rerank: AsyncMock,
    mock_fetch: AsyncMock,
    mock_synthesize_stream: AsyncMock,
):
    """stream=true returns SSE with meta, source, token, done events."""
    mock_search.return_value = SearchResponse(results=[
        SearchResult(title="Stream", url="https://stream.com", snippet="stream"),
    ])
    mock_rerank.return_value = [
        RerankResult(index=0, relevance_score=0.91, text="Stream"),
    ]
    mock_fetch.return_value = MockFetchResult(
        success=True, url="https://stream.com",
        markdown="This is a comprehensive and detailed article that thoroughly covers the subject matter with extensive analysis, multiple examples, and in-depth discussion across many sections. The content provides substantial value through its comprehensive coverage of key topics, detailed explanations of important concepts, and practical insights that make it an excellent source for synthesis and research purposes. Additional paragraphs explore related themes and provide further context and depth.",
        title="Stream", source="crawl4ai", fetch_time_ms=500.0,
    )

    resp = await client.post("/v1/retrieve", json={"query": "stream test", "stream": True, "synthesize": True})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    body = resp.text
    assert "event: meta" in body
    assert "event: source" in body
    assert "event: token" in body
    assert "event: done" in body
    # Meta should contain query and counts
    assert "stream test" in body
    assert '"sources_fetched": 1' in body
    # Source event should contain metadata
    assert "crawl4ai" in body
    assert "0.91" in body


@pytest.mark.anyio
async def test_retrieve_streaming_no_sources(
    client: AsyncClient,
    mock_search: AsyncMock,
    mock_rerank: AsyncMock,
    mock_fetch: AsyncMock,
):
    """stream=true with no sources yields meta + done with no_sources reason."""
    mock_search.return_value = SearchResponse(results=[
        SearchResult(title="Fail", url="https://fail.com", snippet="fail"),
    ])
    mock_rerank.return_value = [
        RerankResult(index=0, relevance_score=0.5, text="Fail"),
    ]
    mock_fetch.return_value = MockFetchResult(
        success=False, url="https://fail.com", error="all tiers exhausted",
    )

    resp = await client.post("/v1/retrieve", json={"query": "fail stream", "stream": True, "synthesize": True})
    assert resp.status_code == 200
    body = resp.text
    assert "event: meta" in body
    assert "event: done" in body
    assert "no_sources" in body
    assert "event: token" not in body or "No sources were available" in body


@pytest.mark.anyio
async def test_retrieve_stream_false_returns_json(
    client: AsyncClient,
    mock_search: AsyncMock,
    mock_rerank: AsyncMock,
    mock_fetch: AsyncMock,
    mock_synthesize: AsyncMock,
):
    """stream=false returns normal JSON RetrieveResponse."""
    mock_search.return_value = SearchResponse(results=[
        SearchResult(title="Json", url="https://json.com", snippet="json"),
    ])
    mock_rerank.return_value = [
        RerankResult(index=0, relevance_score=0.9, text="Json"),
    ]
    mock_fetch.return_value = MockFetchResult(
        success=True, url="https://json.com",
        markdown="This is a comprehensive and detailed article that thoroughly covers the subject matter with extensive analysis, multiple examples, and in-depth discussion across many sections. The content provides substantial value through its comprehensive coverage of key topics, detailed explanations of important concepts, and practical insights that make it an excellent source for synthesis and research purposes. Additional paragraphs explore related themes and provide further context and depth.",
        title="Json",
    )
    mock_synthesize.return_value = (
        "Json answer [1].",
        [Citation(id=1, url="https://json.com", title="Json")],
    )

    resp = await client.post("/v1/retrieve", json={"query": "json test", "stream": False})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    data = resp.json()
    assert data["answer"] == "Json answer [1]."

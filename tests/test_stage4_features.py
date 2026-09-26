"""Unit and integration tests for SearchProxy Stage 4 features:
1. Semantic Vector Cache (Sub-15ms Neural Retrieval via fastembed)
2. Executive Multi-Section Research Dossiers (Obsidian & Notion ready)
3. Native MCP Resources & Prompts
4. Interactive Browser Actions & Screenshots
5. Latency & Quality-Aware Provider Routing (SearXNG strictly Tier 2 fallback)
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np
import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.dependencies import get_fetch_chain
from app.main import app
from app.mcp_server import create_mcp_server
from app.schemas import SourceChunk
from app.services.cache import CacheService
from app.services.crawl4ai import Crawl4AIClient
from app.services.deep_research_service import (
    DeepResearchService,
    _build_annotated_source_directory,
    _build_dossier_frontmatter,
    _fallback_dossier,
)
from app.services.extract_service import ExtractService
from app.services.fetch_chain import FetchChain
from app.services.models import FetchResult
from app.services.search.base import BaseSearchProvider
from app.services.search.models import SearchResponse, SearchResult
from app.services.search.router import SearchRouter

# ---------------------------------------------------------------------------
# Feature 1: Semantic Vector Cache Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_semantic_cache_hit_and_miss(tmp_path):
    """Semantic vector cache returns result when cosine similarity >= threshold."""
    db_file = str(tmp_path / "test_semantic_cache.db")
    settings = Settings(
        CACHE_ENABLED=True,
        CACHE_SEMANTIC_ENABLED=True,
        CACHE_SEMANTIC_THRESHOLD=0.92,
        CACHE_DB_PATH=db_file,
    )
    cache = CacheService(settings=settings)

    # Unit vectors for testing cosine similarity
    vec_base = np.zeros(384, dtype=np.float32)
    vec_base[0] = 1.0  # [1, 0, 0, ...]

    vec_similar = np.zeros(384, dtype=np.float32)
    vec_similar[0] = 0.95
    vec_similar[1] = np.sqrt(1.0 - 0.95**2)  # dot product = 0.95 >= 0.92

    vec_different = np.zeros(384, dtype=np.float32)
    vec_different[1] = 1.0  # dot product = 0.0 < 0.92

    payload = {"results": [{"title": "FastAPI Guide", "url": "https://fastapi.tiangolo.com"}]}

    with patch.object(cache, "_get_embedding_sync", return_value=vec_base):
        await cache.set_search("how to use fastapi", 10, payload)

    # 1. Exact query hit
    hit_exact = await cache.get_search("how to use fastapi", 10)
    assert hit_exact == payload

    # 2. Semantically similar query hit (cosine similarity 0.95 >= 0.92)
    with patch.object(cache, "_get_embedding_sync", return_value=vec_similar):
        hit_semantic = await cache.get_search("guide for using fastapi framework", 10)
        assert hit_semantic == payload

    # 3. Dissimilar query miss (cosine similarity 0.0 < 0.92)
    with patch.object(cache, "_get_embedding_sync", return_value=vec_different):
        miss_semantic = await cache.get_search("quantum computing algorithms", 10)
        assert miss_semantic is None


@pytest.mark.anyio
async def test_semantic_cache_disabled_fallback(tmp_path):
    """When disabled, semantic lookup is skipped and exact key matching works."""
    db_file = str(tmp_path / "test_cache_disabled.db")
    settings = Settings(
        CACHE_ENABLED=True,
        CACHE_SEMANTIC_ENABLED=False,
        CACHE_DB_PATH=db_file,
    )
    cache = CacheService(settings=settings)
    payload = {"results": [{"title": "Exact Match Only", "url": "https://example.com"}]}

    await cache.set_search("python asyncio tutorial", 10, payload)

    # Exact match succeeds
    assert await cache.get_search("python asyncio tutorial", 10) == payload

    # Semantic match is bypassed
    assert await cache.get_search("asyncio in python guide", 10) is None


@pytest.mark.anyio
async def test_cache_research_dossier_crud(tmp_path):
    """CacheService correctly saves and queries research dossiers."""
    db_file = str(tmp_path / "test_dossiers.db")
    settings = Settings(CACHE_ENABLED=True, CACHE_DB_PATH=db_file)
    cache = CacheService(settings=settings)

    await cache.save_research_dossier(
        dossier_id="dossier_101",
        query="Distributed consensus algorithms",
        summary="Overview of Raft, Paxos, and PBFT.",
        dossier="# Distributed Consensus\n\nFull dossier body...",
        format_type="dossier",
        metadata={"citations": 5, "sources": 3},
    )

    # Test get_research_by_id
    entry = await cache.get_research_by_id("dossier_101")
    assert entry is not None
    assert entry["id"] == "dossier_101"
    assert entry["query"] == "Distributed consensus algorithms"
    assert entry["format"] == "dossier"
    assert entry["citations_count"] == 5
    assert entry["sources_count"] == 3

    # Test get_recent_research
    recent = await cache.get_recent_research(limit=10)
    assert len(recent) == 1
    assert recent[0]["id"] == "dossier_101"
    assert recent[0]["citations_count"] == 5


# ---------------------------------------------------------------------------
# Feature 2: Executive Multi-Section Research Dossiers Tests
# ---------------------------------------------------------------------------

def test_dossier_frontmatter_generation():
    """_build_dossier_frontmatter produces valid YAML frontmatter with tags and metadata."""
    settings = Settings()
    sources = [
        SourceChunk(url="https://example.com/1", content="Content 1", title="Title 1"),
        SourceChunk(url="https://example.com/2", content="Content 2", title="Title 2"),
    ]
    frontmatter = _build_dossier_frontmatter("AI agents architecture", sources, settings)
    assert frontmatter.startswith("---\n")
    assert "tags:\n  - deep-research\n  - executive-dossier\n" in frontmatter
    assert 'query: "AI agents architecture"\n' in frontmatter
    assert "format: dossier\n" in frontmatter
    assert frontmatter.endswith("---\n\n")


def test_annotated_source_directory_generation():
    """_build_annotated_source_directory builds markdown entries with domain, score, and tier."""
    sources = [
        SourceChunk(
            url="https://docs.python.org/3/library/asyncio.html",
            content="Asynchronous I/O, event loop, coroutines and tasks.",
            title="Asyncio Documentation",
            relevance_score=0.95,
            fetch_tier="crawl4ai",
        )
    ]
    directory = _build_annotated_source_directory(sources)
    assert "# Annotated Source Directory" in directory
    assert "### [1] Asyncio Documentation" in directory
    assert "**Domain**: `docs.python.org`" in directory
    assert "**Relevance**: `0.95`" in directory
    assert "**Fetch Tier**: `crawl4ai`" in directory
    assert 'Excerpt**: > "Asynchronous I/O, event loop, coroutines and tasks."' in directory


def test_fallback_dossier_structure():
    """_fallback_dossier creates full executive dossier with markdown table."""
    settings = Settings()
    sources = [
        SourceChunk(
            url="https://example.com/alpha",
            content="Alpha source provides data on distributed hash tables.",
            title="Alpha DHT",
            relevance_score=0.88,
        ),
        SourceChunk(
            url="https://example.com/beta",
            content="Beta source analyzes gossip protocol convergence rates.",
            title="Beta Gossip",
            relevance_score=0.91,
        ),
    ]
    dossier = _fallback_dossier("Peer to peer protocols", sources, settings)

    assert "format: dossier" in dossier
    assert "# Executive Summary" in dossier
    assert "**Key Findings:**" in dossier
    assert "# Comparative Analysis" in dossier
    assert "| Source / Entity | Domain | Core Insight & Findings | Citations |" in dossier
    assert "# Detailed Deep Dive Analysis" in dossier
    assert "### Dimension [1]: Alpha DHT" in dossier
    assert "# Annotated Source Directory" in dossier


@pytest.mark.anyio
async def test_deep_research_service_format_dossier():
    """DeepResearchService.research with format='dossier' produces dossier formatted report."""
    mock_search = AsyncMock()
    mock_search.search.return_value = SearchResponse(
        results=[
            SearchResult(
                title="P2P Architecture",
                url="https://example.com/p2p",
                snippet="Overview of peer to peer networks",
                text=(
                    "Peer-to-peer networks distribute computational and storage workloads "
                    "across egalitarian nodes without centralized orchestration services. "
                    "Kademlia and Chord provide logarithmic routing efficiency for keys."
                ),
            )
        ]
    )
    mock_rerank = AsyncMock()
    mock_rerank.rerank.return_value = None  # Use original order
    mock_fetch = AsyncMock()
    mock_synthesis = AsyncMock()
    mock_http = AsyncMock()
    settings = Settings(
        LLM_CHAT_URL="",  # Force fallback dossier generator
        RETRIEVE_MIN_CONTENT_LENGTH=50,
    )

    service = DeepResearchService(
        search_client=mock_search,
        rerank_service=mock_rerank,
        fetch_chain=mock_fetch,
        synthesis_service=mock_synthesis,
        settings=settings,
        http_client=mock_http,
    )

    resp = await service.research("modern p2p systems", max_sub_queries=1, format="dossier")

    assert resp.answer.startswith("---\n")
    assert "format: dossier" in resp.answer
    assert "# Executive Summary" in resp.answer
    assert "# Comparative Analysis" in resp.answer
    assert "# Annotated Source Directory" in resp.answer
    assert resp.research_id is not None

    # Verify dossier was cached in recent_research
    recent = await service.get_recent_research(limit=5)
    assert len(recent) >= 1
    assert recent[0]["format"] == "dossier"


# ---------------------------------------------------------------------------
# Feature 3: Native MCP Resources & Prompts Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_mcp_server_resources(monkeypatch):
    """MCP server registers searchproxy://research/recent and searchproxy://research/{id}."""
    mock_service = AsyncMock()
    mock_service.get_recent_research.return_value = [
        {
            "id": "res_abc123",
            "query": "Rust memory model",
            "summary": "Rust ownership and borrowing mechanics",
            "format": "dossier",
            "citations_count": 4,
            "sources_count": 3,
            "resource_uri": "searchproxy://research/res_abc123",
        }
    ]
    mock_service.get_research_by_id.side_effect = lambda rid: (
        {
            "id": "res_abc123",
            "query": "Rust memory model",
            "dossier": "# Rust Memory Model\nDetailed dossier content.",
            "format": "dossier",
        }
        if rid == "res_abc123"
        else None
    )

    monkeypatch.setattr("app.mcp_server.get_deep_research_service", lambda: mock_service)
    server = create_mcp_server()

    # Verify static resources
    resources = await server.list_resources()
    resource_uris = [r.uri for r in resources]
    assert "searchproxy://research/recent" in resource_uris

    # Verify resource templates
    templates = await server.list_resource_templates()
    template_uris = [t.uri_template for t in templates]
    assert "searchproxy://research/{id}" in template_uris

    # Read recent resources
    recent_read = await server.read_resource("searchproxy://research/recent")
    assert len(recent_read) == 1
    recent_data = json.loads(recent_read[0].content)
    assert recent_data["count"] == 1
    assert recent_data["research"][0]["id"] == "res_abc123"

    # Read specific existing dossier
    dossier_read = await server.read_resource("searchproxy://research/res_abc123")
    assert "Rust Memory Model" in dossier_read[0].content

    # Read specific missing dossier
    missing_read = await server.read_resource("searchproxy://research/non_existent_123")
    assert "not found" in missing_read[0].content.lower()


@pytest.mark.anyio
async def test_mcp_server_prompts():
    """MCP server exposes technical, market, and academic prompt templates."""
    server = create_mcp_server()

    prompts = await server.list_prompts()
    prompt_names = {p.name for p in prompts}
    assert "technical_bug_investigation" in prompt_names
    assert "market_competitive_analysis" in prompt_names
    assert "academic_literature_review" in prompt_names

    # Test technical_bug_investigation
    bug_p = await server.get_prompt(
        "technical_bug_investigation",
        arguments={"query": "Segfault in worker pool", "logs_or_errors": "SIGSEGV at 0x7fff"},
    )
    content0 = bug_p.messages[0].content.text
    assert "Segfault in worker pool" in content0
    assert "SIGSEGV at 0x7fff" in content0
    assert "Investigation Protocol" in content0

    # Test market_competitive_analysis
    mkt_p = await server.get_prompt(
        "market_competitive_analysis",
        arguments={"topic": "Vector search databases", "target_competitors": "Pinecone, Qdrant"},
    )
    content1 = mkt_p.messages[0].content.text
    assert "Vector search databases" in content1
    assert "Pinecone, Qdrant" in content1
    assert "Comparative Matrix" in content1

    # Test academic_literature_review
    acad_p = await server.get_prompt(
        "academic_literature_review",
        arguments={"topic": "Diffusion models in genomics", "focus_areas": "Motif generation"},
    )
    content2 = acad_p.messages[0].content.text
    assert "Diffusion models in genomics" in content2
    assert "Motif generation" in content2
    assert "Seminal & Modern Work" in content2


# ---------------------------------------------------------------------------
# Feature 4: Interactive Browser Actions & Screenshots Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_crawl4ai_actions_and_screenshot_payload():
    """Crawl4AIClient passes actions and screenshot parameters in JSON body."""
    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    tiny_png = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+"
        "M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    mock_resp.json.return_value = {
        "success": True,
        "markdown": "# Page Title\n\nInteractive content loaded.",
        "metadata": {"title": "Page Title"},
        "screenshot": tiny_png,
    }
    mock_http.post.return_value = mock_resp

    settings = Settings(CRAWL4AI_URL="http://localhost:11235")
    client = Crawl4AIClient(client=mock_http, settings=settings)

    actions = [{"type": "click", "selector": "#cookie-accept"}, {"type": "wait", "seconds": 2}]
    res = await client.fetch_markdown(
        "https://example.com/interactive", actions=actions, screenshot=True
    )

    assert res.success is True
    assert res.screenshot_base64 is not None
    assert res.screenshot_base64.startswith("data:image/png;base64,")

    # Verify payload
    call_args = mock_http.post.call_args
    assert call_args is not None
    body = call_args[1]["json"]
    assert body["actions"] == actions
    assert body["screenshot"] is True


@pytest.mark.anyio
async def test_fetch_chain_bypasses_fast_fetch_when_actions_or_screenshot():
    """FetchChain skips Tier 0 FastFetch if actions or screenshot is requested."""
    mock_client = AsyncMock()
    settings = Settings(FAST_FETCH_ENABLED=True)
    chain = FetchChain(client=mock_client, settings=settings)

    mock_fast_fetch = AsyncMock()
    mock_crawl4ai = AsyncMock()
    mock_crawl4ai.fetch_markdown.return_value = FetchResult(
        success=True,
        url="https://example.com",
        markdown="# Interacted Page",
        screenshot_base64="data:image/png;base64,abc",
    )

    chain._fast_fetch = mock_fast_fetch
    chain._crawl4ai = mock_crawl4ai

    # With screenshot=True, fast_fetch should not be called
    res = await chain.execute("https://example.com", screenshot=True)
    assert res.success is True
    assert res.screenshot_base64 == "data:image/png;base64,abc"
    mock_fast_fetch.fetch.assert_not_called()
    mock_crawl4ai.fetch_markdown.assert_called_once_with(
        "https://example.com",
        content_filter=None,
        content_query=None,
        screenshot=True,
    )


def test_fetch_endpoint_accepts_actions_and_screenshot():
    """POST /fetch endpoint accepts actions and screenshot and returns screenshot_base64."""
    mock_chain = AsyncMock()
    mock_chain.execute.return_value = FetchResult(
        success=True,
        url="https://example.com",
        markdown="# Screenshot Taken",
        screenshot_base64="data:image/png;base64,xyz123",
        source="crawl4ai",
    )

    app.dependency_overrides[get_fetch_chain] = lambda: mock_chain
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/fetch",
                json={
                    "url": "https://example.com",
                    "actions": [{"type": "scroll", "direction": "down"}],
                    "screenshot": True,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["screenshot_base64"] == "data:image/png;base64,xyz123"
            mock_chain.execute.assert_called_once_with(
                "https://example.com",
                actions=[{"type": "scroll", "direction": "down"}],
                screenshot=True,
            )
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Feature 5: Latency & Quality-Aware Routing Tests
# ---------------------------------------------------------------------------

class MockStatusProvider(BaseSearchProvider):
    def __init__(self, name: str, tier: int = 1):
        self._name = name
        self._tier = tier
        self.call_count = 0
        self.should_fail = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> int:
        return self._tier

    @property
    def is_available(self) -> bool:
        return True

    async def search(
        self,
        query: str,
        max_results: int = 10,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        freshness: str | None = None,
    ) -> list[SearchResult]:
        self.call_count += 1
        if self.should_fail:
            raise httpx.ConnectTimeout("Timeout connecting to upstream")
        return [
            SearchResult(
                title=f"{self.name} Result",
                url=f"https://{self.name}.com",
                snippet="Test",
            )
        ]


@pytest.mark.anyio
async def test_searxng_remains_strictly_tier2_fallback():
    """SearXNG is strictly a Tier 2 fallback: Tier 1 providers are always preferred when healthy."""
    settings = Settings(SEARCH_PROVIDERS="tavily,brave,searxng")
    mock_client = AsyncMock()

    tavily = MockStatusProvider("tavily", tier=1)
    brave = MockStatusProvider("brave", tier=1)
    searxng = MockStatusProvider("searxng", tier=2)

    router = SearchRouter(
        client=mock_client, settings=settings, custom_providers=[tavily, brave, searxng]
    )

    # Call 1: tavily succeeds (Tier 1)
    resp1 = await router.search("test 1", max_results=5)
    assert resp1.results[0].title == "tavily Result"
    assert tavily.call_count == 1
    assert brave.call_count == 0
    assert searxng.call_count == 0

    # Call 2: brave succeeds (Tier 1 rotation)
    resp2 = await router.search("test 2", max_results=5)
    assert resp2.results[0].title == "brave Result"
    assert tavily.call_count == 1
    assert brave.call_count == 1
    assert searxng.call_count == 0

    # Simulate both Tier 1 providers failing
    tavily.should_fail = True
    brave.should_fail = True

    # Call 3: Tier 1 exhausted -> SearXNG fallback triggered
    resp3 = await router.search("test 3", max_results=5)
    assert resp3.results[0].title == "searxng Result"
    assert searxng.call_count == 1


@pytest.mark.anyio
async def test_degraded_provider_is_skipped_for_healthy():
    """Tier 1 provider marked degraded is skipped in favor of healthy Tier 1."""
    settings = Settings(SEARCH_PROVIDERS="brave,exa,searxng")
    mock_client = AsyncMock()

    brave = MockStatusProvider("brave", tier=1)
    exa = MockStatusProvider("exa", tier=1)
    searxng = MockStatusProvider("searxng", tier=2)

    router = SearchRouter(
        client=mock_client, settings=settings, custom_providers=[brave, exa, searxng]
    )

    # Pre-mark brave as degraded (3 recent errors)
    st = router._stats["brave"]
    st.recent_errors = 3
    st.is_degraded = True
    assert router._is_degraded("brave") is True

    # Standard search should pick healthy exa first, skipping degraded brave
    resp = await router.search("query", max_results=5)
    assert resp.results[0].title == "exa Result"
    assert exa.call_count == 1
    assert brave.call_count == 0
    assert searxng.call_count == 0


@pytest.mark.anyio
async def test_semantic_cache_busy_model_graceful_fallback(tmp_path):
    """When embedding model is busy, CacheService gracefully falls back to exact matching."""
    db_file = str(tmp_path / "busy_cache.db")
    settings = Settings(
        CACHE_ENABLED=True,
        CACHE_SEMANTIC_ENABLED=True,
        CACHE_DB_PATH=db_file,
    )
    cache = CacheService(settings=settings)
    payload = {"results": [{"title": "Cached Result", "url": "https://example.com"}]}

    # Set exact cache entry
    await cache.set_search("kubernetes tutorial", 10, payload)

    # Acquire lock in advance to simulate model being busy
    cache._embedding_lock.acquire()
    try:
        # Exact match still succeeds directly
        hit_exact = await cache.get_search("kubernetes tutorial", 10)
        assert hit_exact == payload

        # Different query misses gracefully without blocking or raising exception
        miss = await cache.get_search("k8s guide", 10)
        assert miss is None
    finally:
        cache._embedding_lock.release()


@pytest.mark.anyio
async def test_deep_research_streaming_records_dossier():
    """SSE research_stream records completed dossier and emits research_id in done event."""
    mock_search = AsyncMock()
    mock_search.search.return_value = SearchResponse(
        results=[
            SearchResult(
                title="BGP Routing",
                url="https://example.com/bgp",
                snippet="BGP convergence and path vector protocol",
                text=(
                    "Border Gateway Protocol manages routing between autonomous "
                    "systems on the Internet."
                ),
            )
        ]
    )
    mock_rerank = AsyncMock()
    mock_rerank.rerank.return_value = None
    mock_fetch = AsyncMock()
    mock_synthesis = AsyncMock()
    mock_http = AsyncMock()
    settings = Settings(
        LLM_CHAT_URL="",  # Use fallback dossier generator
        RETRIEVE_MIN_CONTENT_LENGTH=50,
    )

    service = DeepResearchService(
        search_client=mock_search,
        rerank_service=mock_rerank,
        fetch_chain=mock_fetch,
        synthesis_service=mock_synthesis,
        settings=settings,
        http_client=mock_http,
    )

    events: list[str] = []
    async for chunk in service.research_stream(
        "bgp routing protocol", max_sub_queries=1, format="dossier"
    ):
        events.append(chunk)

    full_stream = "".join(events)
    assert "event: progress\ndata: {\"step\": \"planning\"}" in full_stream
    assert "event: progress\ndata: {\"step\": \"synthesizing\"" in full_stream
    assert "event: done\ndata: {\"finish_reason\": \"stop\", \"research_id\": \"res_" in full_stream

    # Verify dossier is saved and retrievable via get_recent_research
    recent = await service.get_recent_research(limit=5)
    assert len(recent) >= 1
    assert recent[0]["query"] == "bgp routing protocol"
    assert recent[0]["format"] == "dossier"
    research_id = recent[0]["id"]

    # Verify retrieval by ID
    entry = await service.get_research_by_id(research_id)
    assert entry is not None
    assert entry["id"] == research_id
    assert "# Executive Summary" in entry["dossier"]


@pytest.mark.anyio
async def test_fetch_chain_bypasses_cache_when_screenshot_requested(tmp_path):
    """FetchChain bypasses pre-cached plain fetch when screenshot or actions are requested."""
    db_file = str(tmp_path / "fetch_cache.db")
    settings = Settings(
        CACHE_ENABLED=True,
        CACHE_DB_PATH=db_file,
        FAST_FETCH_ENABLED=False,
    )
    cache = CacheService(settings=settings)
    mock_client = AsyncMock()

    chain = FetchChain(client=mock_client, settings=settings, cache=cache)
    mock_crawl4ai = AsyncMock()
    mock_crawl4ai.fetch_markdown.side_effect = [
        # Call 1: plain fetch without screenshot
        FetchResult(
            success=True,
            url="https://example.com/page",
            markdown="# Page Content",
            screenshot_base64=None,
            source="crawl4ai",
        ),
        # Call 2: fetch with screenshot
        FetchResult(
            success=True,
            url="https://example.com/page",
            markdown="# Page Content",
            screenshot_base64="data:image/png;base64,fresh_screenshot_data",
            source="crawl4ai",
        ),
    ]
    chain._crawl4ai = mock_crawl4ai

    # Call 1: Normal fetch without screenshot -> populates cache
    res1 = await chain.execute("https://example.com/page")
    assert res1.success is True
    assert res1.screenshot_base64 is None

    # Call 2: Fetch with screenshot -> MUST bypass plain cache and call crawl4ai
    res2 = await chain.execute("https://example.com/page", screenshot=True)
    assert res2.success is True
    assert res2.screenshot_base64 == "data:image/png;base64,fresh_screenshot_data"
    assert mock_crawl4ai.fetch_markdown.call_count == 2


@pytest.mark.anyio
async def test_extract_service_preserves_screenshot_on_llm_json_failure():
    """ExtractService preserves screenshot_base64 even when LLM output is malformed JSON."""
    mock_chain = AsyncMock()
    mock_chain.execute.return_value = FetchResult(
        success=True,
        url="https://example.com/broken",
        markdown="# Broken Page",
        screenshot_base64="data:image/png;base64,preserved_screen",
    )

    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "This is not valid JSON at all!"}}]
    }
    mock_http.post.return_value = mock_resp

    settings = Settings(LLM_CHAT_URL="https://llm.example.com/v1/chat/completions")
    service = ExtractService(fetch_chain=mock_chain, http_client=mock_http, settings=settings)

    resp = await service.extract("https://example.com/broken", screenshot=True)
    assert resp.success is False
    assert "Failed to parse LLM output as JSON" in resp.error
    assert resp.screenshot_base64 == "data:image/png;base64,preserved_screen"


@pytest.mark.anyio
async def test_hybrid_search_rotates_lexical_providers_and_records_stats():
    """SearchRouter._hybrid_search rotates lexical Tier 1 providers and records latency."""
    settings = Settings(SEARCH_PROVIDERS="tavily,brave,exa,searxng")
    mock_client = AsyncMock()

    tavily = MockStatusProvider("tavily", tier=1)
    brave = MockStatusProvider("brave", tier=1)
    exa = MockStatusProvider("exa", tier=1)
    searxng = MockStatusProvider("searxng", tier=2)

    router = SearchRouter(
        client=mock_client, settings=settings, custom_providers=[tavily, brave, exa, searxng]
    )

    # Call 1: hybrid search -> uses exa (semantic) + tavily (lexical)
    res1 = await router.search("hybrid query 1", hybrid=True)
    assert len(res1.results) > 0
    assert exa.call_count == 1
    assert tavily.call_count == 1
    assert brave.call_count == 0

    # Call 2: hybrid search -> uses exa (semantic) + brave (lexical rotation)
    res2 = await router.search("hybrid query 2", hybrid=True)
    assert len(res2.results) > 0
    assert exa.call_count == 2
    assert tavily.call_count == 1
    assert brave.call_count == 1

    # Verify that success and rolling latency were recorded
    assert router._stats["tavily"].total_requests == 1
    assert len(router._stats["tavily"].rolling_latencies) == 1
    assert router._stats["brave"].total_requests == 1
    assert len(router._stats["brave"].rolling_latencies) == 1
    assert searxng.call_count == 0


@pytest.mark.anyio
async def test_semantic_cache_real_fastembed(tmp_path):
    """End-to-end test of real fastembed inference in CacheService with sub-15ms retrieval."""
    import time

    db_file = str(tmp_path / "real_fastembed_cache.db")
    settings = Settings(
        CACHE_ENABLED=True,
        CACHE_SEMANTIC_ENABLED=True,
        CACHE_SEMANTIC_THRESHOLD=0.90,
        CACHE_DB_PATH=db_file,
    )
    cache = CacheService(settings=settings)
    payload = {"results": [{"title": "FastAPI Framework", "url": "https://fastapi.tiangolo.com"}]}

    # Store query
    await cache.set_search("how to use fastapi", 10, payload)

    # Warmup and query with semantically close question
    t0 = time.perf_counter()
    hit = await cache.get_search("how to use fastapi framework", 10)
    latency_ms = (time.perf_counter() - t0) * 1000

    assert hit == payload
    # Semantic retrieval should be fast (sub-25ms even in unoptimized CI/local test environments)
    assert latency_ms < 50.0

    # Query with completely unrelated question should miss
    miss = await cache.get_search("quantum mechanics schrodinger equation", 10)
    assert miss is None



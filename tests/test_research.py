"""Tests for Native 2-Hop Deep Research Service & /v1/research endpoint."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.dependencies import get_deep_research_service
from app.main import app as fastapi_app
from app.schemas import Citation, RetrieveResponse, SourceChunk
from app.services.deep_research_service import DeepResearchService
from app.services.search.models import SearchResponse, SearchResult


@pytest.fixture
def mock_deep_research_service():
    mock = MagicMock()
    mock.research = AsyncMock(
        return_value=RetrieveResponse(
            query="test query",
            answer="# Deep Research Report\nComprehensive findings [1].",
            report="# Deep Research Report\nComprehensive findings [1].",
            citations=[Citation(id=1, url="https://example.com", title="Example")],
            sources=[
                SourceChunk(
                    url="https://example.com",
                    title="Example",
                    content="Example text",
                    fetch_tier="crawl4ai",
                )
            ],
            sources_fetched=1,
            sources_failed=0,
        )
    )

    async def _mock_stream(**kwargs):
        yield f"event: progress\ndata: {json.dumps({'step': 'planning'})}\n\n"
        yield f"event: progress\ndata: {json.dumps({'step': 'hop_1'})}\n\n"
        yield f"event: progress\ndata: {json.dumps({'step': 'gap_analysis'})}\n\n"
        yield f"event: progress\ndata: {json.dumps({'step': 'hop_2'})}\n\n"
        yield f"event: progress\ndata: {json.dumps({'step': 'synthesizing'})}\n\n"
        yield f"event: token\ndata: {json.dumps('# Report Chunk')}\n\n"
        yield f"event: done\ndata: {json.dumps({'finish_reason': 'stop'})}\n\n"

    mock.research_stream = _mock_stream
    return mock


@pytest.fixture
async def client(mock_deep_research_service):
    fastapi_app.dependency_overrides[get_deep_research_service] = lambda: mock_deep_research_service
    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as ac:
        yield ac
    fastapi_app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_v1_research_endpoint(client, mock_deep_research_service):
    """POST /v1/research executes deep research and returns full RetrieveResponse."""
    resp = await client.post(
        "/v1/research", json={"query": "quantum computing breakthroughs", "fetch_top_k": 5}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "Deep Research Report" in data["answer"]
    # Check backward compatibility: report key exists and matches answer
    assert data["report"] == data["answer"]
    assert len(data["citations"]) == 1
    assert data["citations"][0]["url"] == "https://example.com"
    mock_deep_research_service.research.assert_awaited_once()


@pytest.mark.anyio
async def test_research_messages_payload(client, mock_deep_research_service):
    """POST /v1/research extracts query from messages array."""
    resp = await client.post(
        "/v1/research",
        json={
            "messages": [
                {"role": "system", "content": "You are a helper"},
                {"role": "user", "content": "extracted query topic"},
            ]
        },
    )
    assert resp.status_code == 200
    mock_deep_research_service.research.assert_awaited_once()


@pytest.mark.anyio
async def test_research_streaming_endpoint(client, mock_deep_research_service):
    """POST /v1/research with stream=true yields SSE progress steps and tokens."""
    resp = await client.post(
        "/v1/research",
        json={"query": "stream deep research", "stream": True},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert '"step": "planning"' in body
    assert '"step": "hop_1"' in body
    assert '"step": "gap_analysis"' in body
    assert '"step": "hop_2"' in body
    assert '"step": "synthesizing"' in body
    assert "event: token" in body
    assert "event: done" in body


@pytest.mark.anyio
async def test_deep_research_service_unit():
    """Unit test for DeepResearchService logic: decomposition, gap analysis, and 2-hop loop."""
    settings = Settings(
        LLM_CHAT_URL="https://api.openai.com/v1/chat/completions",
        LLM_CHAT_MODEL="gpt-4o-mini",
        LLM_API_KEY="test-key",
        RETRIEVE_MIN_CONTENT_LENGTH=50,
    )
    mock_search = AsyncMock()
    mock_rerank = AsyncMock()
    mock_fetch = AsyncMock()
    mock_synthesis = AsyncMock()
    mock_http = AsyncMock()

    service = DeepResearchService(
        search_client=mock_search,
        rerank_service=mock_rerank,
        fetch_chain=mock_fetch,
        synthesis_service=mock_synthesis,
        settings=settings,
        http_client=mock_http,
    )

    # 1. Test decompose_query
    mock_http.post.return_value = httpx.Response(
        status_code=200,
        json={"choices": [{"message": {"content": '["subquery 1", "subquery 2"]'}}]},
        request=httpx.Request("POST", "https://api.openai.com"),
    )
    sub_queries = await service.decompose_query("primary query")
    assert sub_queries == ["subquery 1", "subquery 2"]

    # 2. Test gap_analysis
    mock_http.post.return_value = httpx.Response(
        status_code=200,
        json={"choices": [{"message": {"content": '["gap follow-up 1"]'}}]},
        request=httpx.Request("POST", "https://api.openai.com"),
    )
    sources = [
        SourceChunk(
            url="https://ex.com",
            title="Ex",
            content="Some initial content that is long enough.",
        )
    ]
    gap_queries = await service.gap_analysis("primary query", sources)
    assert gap_queries == ["gap follow-up 1"]

    # 3. Test research end-to-end
    mock_search.search.side_effect = [
        # Hop 1 subqueries
        SearchResponse(
            results=[
                SearchResult(
                    title="Doc 1",
                    url="https://doc1.com",
                    snippet="Doc 1 snippet",
                    text=(
                        "Full text for Doc 1 about quantum breakthroughs "
                        "and experimental results."
                    ),
                ),
            ]
        ),
        SearchResponse(results=[]),
        SearchResponse(results=[]),
        # Hop 2 gap query
        SearchResponse(
            results=[
                SearchResult(
                    title="Doc 2",
                    url="https://doc2.com",
                    snippet="Doc 2 snippet",
                    text="Full text for Doc 2 about quantum error correction and fault tolerance.",
                ),
            ]
        ),
    ]

    mock_rerank.rerank.return_value = []
    # Mock LLM for synthesis report
    mock_http.post.side_effect = [
        # decompose
        httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": '["sub 1"]'}}]},
            request=httpx.Request("POST", "https://api.openai.com"),
        ),
        # gap analysis
        httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": '["gap 1"]'}}]},
            request=httpx.Request("POST", "https://api.openai.com"),
        ),
        # synthesis report
        httpx.Response(
            status_code=200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "# Report\nQuantum breakthroughs were achieved [1]. "
                                "Error correction works [2]."
                            )
                        }
                    }
                ]
            },
            request=httpx.Request("POST", "https://api.openai.com"),
        ),
    ]

    result = await service.research(query="quantum breakthroughs", fetch_top_k=4)
    assert result.query == "quantum breakthroughs"
    assert "Quantum breakthroughs" in result.answer
    assert result.report == result.answer
    assert len(result.sources) >= 1
    # Check that citations were verified and matched
    assert len(result.citations) >= 1

"""Tests for Native Deep Research Service & /v1/research and /vane endpoints."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_deep_research_service
from app.main import app as fastapi_app
from app.schemas import Citation, RetrieveResponse, SourceChunk


@pytest.fixture
def mock_deep_research_service():
    mock = MagicMock()
    mock.research = AsyncMock(
        return_value=RetrieveResponse(
            query="test query",
            answer="# Deep Research Report\nComprehensive findings [1].",
            citations=[Citation(id=1, url="https://example.com", title="Example")],
            sources=[SourceChunk(url="https://example.com", title="Example", content="Example text", fetch_tier="crawl4ai")],
            sources_fetched=1,
            sources_failed=0,
        )
    )
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
    resp = await client.post("/v1/research", json={"query": "quantum computing breakthroughs", "fetch_top_k": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert "Deep Research Report" in data["answer"]
    assert len(data["citations"]) == 1
    assert data["citations"][0]["url"] == "https://example.com"
    mock_deep_research_service.research.assert_awaited_once()


@pytest.mark.anyio
async def test_legacy_vane_endpoint(client, mock_deep_research_service):
    """POST /vane executes deep research and returns legacy report shape."""
    resp = await client.post("/vane", json={"query": "quantum computing breakthroughs"})
    assert resp.status_code == 200
    data = resp.json()
    assert "report" in data
    assert "Deep Research Report" in data["report"]


@pytest.mark.anyio
async def test_research_messages_payload(client, mock_deep_research_service):
    """POST /v1/research extracts query from messages array."""
    resp = await client.post("/v1/research", json={
        "messages": [
            {"role": "system", "content": "You are a helper"},
            {"role": "user", "content": "extracted query topic"},
        ]
    })
    assert resp.status_code == 200
    mock_deep_research_service.research.assert_awaited_once()

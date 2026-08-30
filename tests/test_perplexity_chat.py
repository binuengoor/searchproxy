"""Tests for /compat/perplexity/chat/completions and /v1/chat/completions."""
from __future__ import annotations

from unittest.mock import AsyncMock
import pytest
from httpx import AsyncClient

from app.dependencies import get_retrieve_service
from app.main import app as fastapi_app
from app.schemas import Citation, RetrieveResponse, SourceChunk


@pytest.fixture
def mock_retrieve_service():
    mock = AsyncMock()
    mock.retrieve.return_value = RetrieveResponse(
        query="Test query",
        answer="This is a synthesized test answer [1].",
        citations=[Citation(id=1, url="https://example.com/test", title="Test Page", relevance_score=0.95)],
        sources=[SourceChunk(url="https://example.com/test", title="Test Page", content="Content", fetch_tier="crawl4ai", relevance_score=0.95)],
        sources_fetched=1,
        sources_failed=0,
    )
    fastapi_app.dependency_overrides[get_retrieve_service] = lambda: mock
    yield mock
    fastapi_app.dependency_overrides.pop(get_retrieve_service, None)


@pytest.mark.anyio
async def test_perplexity_chat_completions_json(client: AsyncClient, mock_retrieve_service):
    """Test non-streaming /compat/perplexity/chat/completions returns OpenAI-style response."""
    payload = {
        "model": "sonar",
        "messages": [{"role": "user", "content": "What is the capital of France?"}],
        "stream": False,
    }
    resp = await client.post("/compat/perplexity/chat/completions", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "sonar"
    assert data["choices"][0]["message"]["content"] == "This is a synthesized test answer [1]."
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["citations"] == ["https://example.com/test"]


@pytest.mark.anyio
async def test_v1_chat_completions_alias(client: AsyncClient, mock_retrieve_service):
    """Test /v1/chat/completions alias."""
    payload = {
        "model": "sonar-pro",
        "messages": [{"role": "user", "content": "Tell me about quantum computing."}],
        "stream": False,
    }
    resp = await client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "This is a synthesized test answer [1]."

@pytest.mark.anyio
async def test_perplexity_chat_completions_streaming(client: AsyncClient, mock_retrieve_service):
    """Test SSE streaming /compat/perplexity/chat/completions."""
    async def mock_stream(*args, **kwargs):
        yield 'event: source\ndata: {"url": "https://example.com/stream"}\n\n'
        yield 'event: token\ndata: "Streaming "\n\n'
        yield 'event: token\ndata: "content."\n\n'
        yield 'event: done\ndata: {"finish_reason": "stop"}\n\n'

    mock_retrieve_service.retrieve_stream = mock_stream

    payload = {
        "model": "sonar",
        "messages": [{"role": "user", "content": "Stream this"}],
        "stream": True,
    }
    resp = await client.post("/compat/perplexity/chat/completions", json=payload)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body_text = resp.text
    assert "Streaming " in body_text
    assert "content." in body_text
    assert "[DONE]" in body_text

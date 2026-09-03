"""Tests for Local ONNX Reranker and RerankService fallback logic."""

from __future__ import annotations

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.config import Settings
from app.services.local_reranker import LocalReranker
from app.services.rerank_service import RerankResult, RerankService


@pytest.fixture
def rerank_settings():
    return Settings(
        RERANK_LOCAL=True,
        LOCAL_RERANK_MODEL="BAAI/bge-reranker-base",
        CF_RERANK_URL="https://cf-inference.example.com/v1/rerank",
        CF_RERANK_API_KEY="test-cf-key",
    )


@pytest.mark.asyncio
async def test_local_reranker_success(rerank_settings):
    mock_local = MagicMock(spec=LocalReranker)
    # Return raw similarity scores: doc 2 is most relevant, doc 0 second, doc 1 least
    mock_local.rerank = AsyncMock(return_value=[2.5, -4.0, 8.1])

    service = RerankService(
        client=MagicMock(spec=httpx.AsyncClient),
        settings=rerank_settings,
        local_reranker=mock_local,
    )

    docs = ["doc0 python", "doc1 cooking", "doc2 concurrency"]
    results = await service.rerank("concurrency in python", docs, top_k=2)

    assert results is not None
    assert len(results) == 2
    # Top 1 should be doc2 (score 8.1)
    assert results[0].index == 2
    assert results[0].relevance_score == 8.1
    assert results[0].text == "doc2 concurrency"

    # Top 2 should be doc0 (score 2.5)
    assert results[1].index == 0
    assert results[1].relevance_score == 2.5


@pytest.mark.asyncio
async def test_local_rerank_fallback_to_remote_on_error(rerank_settings):
    mock_local = MagicMock(spec=LocalReranker)
    mock_local.rerank = AsyncMock(side_effect=RuntimeError("ONNX engine failed"))

    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [
            {"index": 1, "relevance_score": 0.95, "document": {"text": "remote doc 1"}},
            {"index": 0, "relevance_score": 0.80, "document": {"text": "remote doc 0"}},
        ]
    }
    client.post = AsyncMock(return_value=mock_resp)

    service = RerankService(
        client=client,
        settings=rerank_settings,
        local_reranker=mock_local,
    )

    docs = ["doc0", "doc1"]
    results = await service.rerank("query", docs)

    # Verify fallback to remote happened
    assert results is not None
    assert len(results) == 2
    assert results[0].index == 1
    assert results[0].relevance_score == 0.95
    client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_rerank_returns_none_when_both_fail(rerank_settings):
    mock_local = MagicMock(spec=LocalReranker)
    mock_local.rerank = AsyncMock(side_effect=RuntimeError("Local failed"))

    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(side_effect=httpx.ConnectError("Remote unreachable"))

    service = RerankService(
        client=client,
        settings=rerank_settings,
        local_reranker=mock_local,
    )

    results = await service.rerank("query", ["doc0", "doc1"])
    assert results is None

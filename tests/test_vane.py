from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.main import app as fastapi_app
from app.routers.vane import _get_vane_client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def mock_vane_client():
    """Fresh mock VaneProxyClient for each test."""
    mock = MagicMock()
    mock.research = AsyncMock(return_value="")

    async def _empty_stream(query, depth):
        return
        yield

    mock.research_stream = _empty_stream
    return mock


@pytest.fixture
async def client(mock_vane_client, monkeypatch):
    """httpx AsyncClient against the FastAPI app with a mocked VaneProxyClient."""
    import httpx

    _real_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    monkeypatch.setattr("app.main._client", _real_client)

    fastapi_app.dependency_overrides[_get_vane_client] = lambda: mock_vane_client

    from httpx import ASGITransport

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fastapi_app), base_url="http://test"
    ) as ac:
        yield ac

    fastapi_app.dependency_overrides.clear()
    await _real_client.aclose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_sync_research_returns_report(client, mock_vane_client):
    """POST /vane with a valid body returns 200 and a report string."""
    expected = "This is the synthesized research report."
    mock_vane_client.research = AsyncMock(return_value=expected)

    response = await client.post("/vane", json={"query": "test", "depth": "concise"})

    assert response.status_code == 200
    data = response.json()
    assert data["report"] == expected
    mock_vane_client.research.assert_awaited_once_with(query="test", depth="concise")


@pytest.mark.anyio
async def test_sync_research_empty_string_is_200(client, mock_vane_client):
    """When research() returns '' the endpoint still returns 200 with report: ''."""
    mock_vane_client.research = AsyncMock(return_value="")

    response = await client.post("/vane", json={"query": "anything", "depth": "balanced"})

    assert response.status_code == 200
    data = response.json()
    assert data["report"] == ""


@pytest.mark.anyio
async def test_depth_mapping(client, mock_vane_client):
    """Depth values are forwarded unchanged from the request body to research()."""
    mock_vane_client.research = AsyncMock(return_value="depth test report")

    for depth in ("concise", "balanced", "comprehensive"):
        mock_vane_client.research.reset_mock()
        response = await client.post("/vane", json={"query": "depth check", "depth": depth})
        assert response.status_code == 200
        mock_vane_client.research.assert_awaited_once_with(
            query="depth check", depth=depth
        )


@pytest.mark.anyio
async def test_streaming_returns_chunks(client, mock_vane_client):
    """POST /vane?stream=true returns a streaming text response."""

    async def fake_stream(query, depth):
        yield "chunk one"
        yield "chunk two"
        yield "chunk three"

    mock_vane_client.research_stream = fake_stream

    response = await client.post(
        "/vane?stream=true", json={"query": "stream test", "depth": "concise"}
    )

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]

    accumulated = "".join([c async for c in response.aiter_text()])
    assert accumulated == "chunk onechunk twochunk three"


@pytest.mark.anyio
async def test_streaming_empty_generator(client, mock_vane_client):
    """Streaming an empty generator still returns 200 with an empty body."""

    async def empty_stream(query, depth):
        return
        yield

    mock_vane_client.research_stream = empty_stream

    response = await client.post(
        "/vane?stream=true", json={"query": "empty", "depth": "balanced"}
    )

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]

    chunks = []
    async for chunk in response.aiter_text():
        chunks.append(chunk)

    assert chunks == []


@pytest.mark.anyio
async def test_default_depth_is_balanced(client, mock_vane_client):
    """When depth is omitted it defaults to 'balanced' in the request body."""
    mock_vane_client.research = AsyncMock(return_value="default depth report")

    response = await client.post("/vane", json={"query": "default depth test"})

    assert response.status_code == 200
    mock_vane_client.research.assert_awaited_once_with(
        query="default depth test", depth="balanced"
    )

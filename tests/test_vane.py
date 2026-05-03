from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.main import app as fastapi_app
from app.dependencies import get_vane_client
from app.services.vane_proxy import VaneTimeoutError, VaneUpstreamError, VaneError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def mock_vane_client():
    """Fresh mock VaneProxyClient for each test."""
    mock = MagicMock()
    mock.research = AsyncMock(return_value="default report")

    async def _empty_stream(query, optimization_mode):
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

    fastapi_app.dependency_overrides[get_vane_client] = lambda: mock_vane_client

    from httpx import ASGITransport

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fastapi_app), base_url="http://test"
    ) as ac:
        yield ac

    fastapi_app.dependency_overrides.clear()
    await _real_client.aclose()


# ---------------------------------------------------------------------------
# Success tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_sync_research_returns_report(client, mock_vane_client):
    """POST /vane with a valid body returns 200 and a report string."""
    expected = "This is the synthesized research report."
    mock_vane_client.research = AsyncMock(return_value=expected)

    response = await client.post("/vane", json={"query": "test", "optimization_mode": "speed"})

    assert response.status_code == 200
    data = response.json()
    assert data["report"] == expected
    mock_vane_client.research.assert_awaited_once_with(query="test", optimization_mode="speed")


@pytest.mark.anyio
async def test_optimization_mode_forwarding(client, mock_vane_client):
    """optimization_mode values are forwarded unchanged from the request body to research()."""
    mock_vane_client.research = AsyncMock(return_value="mode test report")

    for mode in ("speed", "balanced", "quality"):
        mock_vane_client.research.reset_mock()
        response = await client.post("/vane", json={"query": "mode check", "optimization_mode": mode})
        assert response.status_code == 200
        mock_vane_client.research.assert_awaited_once_with(
            query="mode check", optimization_mode=mode
        )


@pytest.mark.anyio
async def test_streaming_returns_chunks(client, mock_vane_client):
    """POST /vane?stream=true returns a streaming text response."""

    async def fake_stream(query, optimization_mode):
        yield "chunk one"
        yield "chunk two"
        yield "chunk three"

    mock_vane_client.research_stream = fake_stream

    response = await client.post(
        "/vane?stream=true", json={"query": "stream test", "optimization_mode": "speed"}
    )

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]

    accumulated = "".join([c async for c in response.aiter_text()])
    assert accumulated == "chunk onechunk twochunk three"


@pytest.mark.anyio
async def test_streaming_empty_generator(client, mock_vane_client):
    """Streaming an empty generator still returns 200 with an empty body."""

    async def empty_stream(query, optimization_mode):
        return
        yield

    mock_vane_client.research_stream = empty_stream

    response = await client.post(
        "/vane?stream=true", json={"query": "empty", "optimization_mode": "balanced"}
    )

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]

    chunks = []
    async for chunk in response.aiter_text():
        chunks.append(chunk)

    assert chunks == []


@pytest.mark.anyio
async def test_default_optimization_mode_is_balanced(client, mock_vane_client):
    """When optimization_mode is omitted it defaults to 'balanced'."""
    mock_vane_client.research = AsyncMock(return_value="default mode report")

    response = await client.post("/vane", json={"query": "default mode test"})

    assert response.status_code == 200
    mock_vane_client.research.assert_awaited_once_with(
        query="default mode test", optimization_mode="balanced"
    )


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_vane_timeout_returns_error_report(client, mock_vane_client):
    """When Vane times out, the endpoint returns 200 with an error message in the report."""
    mock_vane_client.research = AsyncMock(
        side_effect=VaneTimeoutError("Vane research timed out after 120s for query='test'")
    )

    response = await client.post("/vane", json={"query": "test"})

    assert response.status_code == 200
    data = response.json()
    assert "unavailable" in data["report"].lower()
    assert "timed out" in data["report"].lower()


@pytest.mark.anyio
async def test_vane_upstream_error_returns_error_report(client, mock_vane_client):
    """When Vane returns HTTP 500, the endpoint returns 200 with an error message."""
    mock_vane_client.research = AsyncMock(
        side_effect=VaneUpstreamError("Vane returned HTTP 500 for query='test'", status_code=500)
    )

    response = await client.post("/vane", json={"query": "test"})

    assert response.status_code == 200
    data = response.json()
    assert "unavailable" in data["report"].lower()
    assert "500" in data["report"]


@pytest.mark.anyio
async def test_vane_generic_error_returns_error_report(client, mock_vane_client):
    """When Vane has a connection error, the endpoint returns 200 with an error message."""
    mock_vane_client.research = AsyncMock(
        side_effect=VaneError("Vane request failed for query='test': Connection refused")
    )

    response = await client.post("/vane", json={"query": "test"})

    assert response.status_code == 200
    data = response.json()
    assert "unavailable" in data["report"].lower()
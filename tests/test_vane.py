from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.main import app as fastapi_app
from app.dependencies import get_vane_client
from app.services.vane_proxy import (
    VaneProxyClient,
    VaneTimeoutError,
    VaneUpstreamError,
    VaneError,
)

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
# Error handling tests (router level)
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


# ---------------------------------------------------------------------------
# Retry tests (service level)
# ---------------------------------------------------------------------------

class FakeResponse:
    """Minimal httpx.Response stand-in for mocking status-code errors."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        raise httpx.HTTPStatusError(
            message=f"Server error {self.status_code}",
            request=MagicMock(),
            response=self,
        )


@pytest.fixture
def dummy_settings():
    """Return a Settings-like object with minimal VANE_* attributes."""
    from types import SimpleNamespace

    return SimpleNamespace(
        VANE_URL="http://vane-test:3001",
        VANE_CHAT_PROVIDER_ID="p1",
        VANE_CHAT_MODEL_KEY="m1",
        VANE_EMBED_PROVIDER_ID="p2",
        VANE_EMBED_MODEL_KEY="m2",
        VANE_TIMEOUT=120,
    )


@pytest.mark.anyio
async def test_research_retries_on_500_then_succeeds(dummy_settings, monkeypatch):
    """VaneProxyClient retries on HTTP 500 and succeeds on the second attempt."""
    mock_post = AsyncMock(side_effect=[
        FakeResponse(500),
        MagicMock(raise_for_status=lambda: None, json=lambda: {"message": "retried report"}),
    ])
    mock_sleep = AsyncMock()

    monkeypatch.setattr("app.services.vane_proxy.asyncio.sleep", mock_sleep)

    proxy = VaneProxyClient(client=MagicMock(post=mock_post), settings=dummy_settings)
    result = await proxy.research("retry test")

    assert result == "retried report"
    assert mock_post.await_count == 2
    mock_sleep.assert_awaited_once_with(1.0)


@pytest.mark.anyio
async def test_research_fails_after_3_retries_on_500(dummy_settings, monkeypatch):
    """VaneProxyClient gives up after 3 consecutive HTTP 500s."""
    mock_post = AsyncMock(side_effect=[
        FakeResponse(500),
        FakeResponse(500),
        FakeResponse(500),
    ])
    mock_sleep = AsyncMock()
    monkeypatch.setattr("app.services.vane_proxy.asyncio.sleep", mock_sleep)

    proxy = VaneProxyClient(client=MagicMock(post=mock_post), settings=dummy_settings)

    with pytest.raises(VaneUpstreamError) as exc_info:
        await proxy.research("always 500")

    assert exc_info.value.status_code == 500
    assert mock_post.await_count == 3
    assert mock_sleep.await_count == 2  # sleeps after attempts 1 and 2


@pytest.mark.anyio
async def test_research_no_retry_on_4xx(dummy_settings, monkeypatch):
    """VaneProxyClient does NOT retry on HTTP 400 (client error)."""
    mock_post = AsyncMock(side_effect=[FakeResponse(400)])
    mock_sleep = AsyncMock()
    monkeypatch.setattr("app.services.vane_proxy.asyncio.sleep", mock_sleep)

    proxy = VaneProxyClient(client=MagicMock(post=mock_post), settings=dummy_settings)

    with pytest.raises(VaneUpstreamError) as exc_info:
        await proxy.research("bad request")

    assert exc_info.value.status_code == 400
    assert mock_post.await_count == 1
    mock_sleep.assert_not_awaited()


@pytest.mark.anyio
async def test_research_no_retry_on_timeout(dummy_settings, monkeypatch):
    """VaneProxyClient does NOT retry on httpx.TimeoutException."""
    mock_post = AsyncMock(side_effect=httpx.TimeoutException("Connection timed out"))
    mock_sleep = AsyncMock()
    monkeypatch.setattr("app.services.vane_proxy.asyncio.sleep", mock_sleep)

    proxy = VaneProxyClient(client=MagicMock(post=mock_post), settings=dummy_settings)

    with pytest.raises(VaneTimeoutError):
        await proxy.research("slow query")

    assert mock_post.await_count == 1
    mock_sleep.assert_not_awaited()


@pytest.mark.anyio
async def test_stream_retries_on_502_then_succeeds(dummy_settings, monkeypatch):
    """VaneProxyClient research_stream retries on HTTP 502 and succeeds."""

    async def _ok_aiter():
        for chunk in ("chunk1", "chunk2"):
            yield chunk

    ok_response = MagicMock()
    ok_response.raise_for_status = MagicMock()
    ok_response.aiter_text = _ok_aiter

    mock_stream = MagicMock()
    mock_stream.__aenter__ = AsyncMock(side_effect=[
        FakeResponse(502),
        ok_response,
    ])
    mock_stream.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=mock_stream)

    mock_sleep = AsyncMock()
    monkeypatch.setattr("app.services.vane_proxy.asyncio.sleep", mock_sleep)

    proxy = VaneProxyClient(client=mock_client, settings=dummy_settings)
    chunks = [c async for c in proxy.research_stream("stream retry")]

    assert chunks == ["chunk1", "chunk2"]
    assert mock_client.stream.call_count == 2
    mock_sleep.assert_awaited_once_with(1.0)


@pytest.mark.anyio
async def test_stream_gives_up_after_3_retries(dummy_settings, monkeypatch):
    """VaneProxyClient research_stream yields error after 3 consecutive 503s."""
    mock_stream = MagicMock()
    mock_stream.__aenter__ = AsyncMock(side_effect=[
        FakeResponse(503),
        FakeResponse(503),
        FakeResponse(503),
    ])
    mock_stream.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=mock_stream)

    mock_sleep = AsyncMock()
    monkeypatch.setattr("app.services.vane_proxy.asyncio.sleep", mock_sleep)

    proxy = VaneProxyClient(client=mock_client, settings=dummy_settings)
    chunks = [c async for c in proxy.research_stream("stream fail")]

    assert chunks == ["[Vane stream error: HTTP 503]"]
    assert mock_client.stream.call_count == 3
    assert mock_sleep.await_count == 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

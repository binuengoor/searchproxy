"""Tests for Byparr / FlareSolverr client."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import Settings
from app.services.byparr_client import ByparrClient


@pytest.fixture
def byparr_settings():
    return Settings(
        BYPARR_URL="http://byparr:8191/v1",
        BYPARR_TIMEOUT=30.0,
    )


def test_byparr_is_configured(byparr_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    byparr = ByparrClient(client=client, settings=byparr_settings)
    assert byparr.is_configured() is True

    unconfigured_settings = Settings(BYPARR_URL="")
    byparr_unconfigured = ByparrClient(client=client, settings=unconfigured_settings)
    assert byparr_unconfigured.is_configured() is False


@pytest.mark.asyncio
async def test_byparr_fetch_unconfigured():
    client = MagicMock(spec=httpx.AsyncClient)
    settings = Settings(BYPARR_URL="")
    byparr = ByparrClient(client=client, settings=settings)

    result = await byparr.fetch("https://example.com/protected")
    assert result.success is False
    assert result.url == "https://example.com/protected"
    assert result.source == "byparr"
    assert "not configured" in result.error
    assert result.markdown == ""


@pytest.mark.asyncio
async def test_byparr_fetch_success(byparr_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    expected_html = (
        "<html><body><h1>Solved Title</h1>"
        "<p>Bypassed Cloudflare turnstile content.</p></body></html>"
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "ok",
        "message": "Challenge solved",
        "solution": {
            "url": "https://example.com/protected",
            "status": 200,
            "response": expected_html,
        },
    }
    client.post = AsyncMock(return_value=mock_resp)

    byparr = ByparrClient(client=client, settings=byparr_settings)
    result = await byparr.fetch("https://example.com/protected")

    assert result.success is True
    assert result.url == "https://example.com/protected"
    assert result.status_code == 200
    assert result.source == "byparr"
    assert result.markdown == expected_html
    assert len(result.markdown) > 0

    # Verify request payload
    client.post.assert_awaited_once()
    call_args = client.post.call_args
    assert call_args.args[0] == "http://byparr:8191/v1"
    assert call_args.kwargs["json"]["cmd"] == "request.get"
    assert call_args.kwargs["json"]["url"] == "https://example.com/protected"
    assert call_args.kwargs["json"]["maxTimeout"] == 30000


@pytest.mark.asyncio
async def test_byparr_fetch_http_error(byparr_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 502
    client.post = AsyncMock(return_value=mock_resp)

    byparr = ByparrClient(client=client, settings=byparr_settings)
    result = await byparr.fetch("https://example.com/protected")

    assert result.success is False
    assert result.status_code == 502
    assert "HTTP 502" in result.error
    assert result.markdown == ""


@pytest.mark.asyncio
async def test_byparr_fetch_status_error(byparr_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "error",
        "message": "Error: Unable to resolve challenge",
    }
    client.post = AsyncMock(return_value=mock_resp)

    byparr = ByparrClient(client=client, settings=byparr_settings)
    result = await byparr.fetch("https://example.com/protected")

    assert result.success is False
    assert "Unable to resolve challenge" in result.error
    assert result.markdown == ""


@pytest.mark.asyncio
async def test_byparr_fetch_empty_response_html(byparr_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "ok",
        "solution": {
            "status": 200,
            "response": "",
        },
    }
    client.post = AsyncMock(return_value=mock_resp)

    byparr = ByparrClient(client=client, settings=byparr_settings)
    result = await byparr.fetch("https://example.com/protected")

    assert result.success is False
    assert "empty response HTML" in result.error
    assert result.markdown == ""


@pytest.mark.asyncio
async def test_byparr_fetch_timeout(byparr_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))

    byparr = ByparrClient(client=client, settings=byparr_settings)
    result = await byparr.fetch("https://example.com/protected")

    assert result.success is False
    assert "timed out" in result.error
    assert result.markdown == ""


@pytest.mark.asyncio
async def test_byparr_fetch_generic_exception(byparr_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(side_effect=RuntimeError("Connection reset"))

    byparr = ByparrClient(client=client, settings=byparr_settings)
    result = await byparr.fetch("https://example.com/protected")

    assert result.success is False
    assert "Connection reset" in result.error
    assert result.markdown == ""

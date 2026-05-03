"""pytest tests for the POST /fetch HTTP endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.main import app as fastapi_app
from app.dependencies import get_fetch_chain


@pytest.fixture
def mock_fetch_chain():
    """Override the get_fetch_chain dependency with a mock FetchChain."""
    chain = AsyncMock()
    fastapi_app.dependency_overrides[get_fetch_chain] = lambda: chain
    yield chain
    fastapi_app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_fetch_success(client, mock_fetch_chain):
    """POST /fetch returns 200 with a successful FetchResult."""
    mock_fetch_chain.execute.return_value = {
        "success": True,
        "url": "https://example.com",
        "markdown": "# Example Domain\n\nThis domain is for use in illustrative examples.",
        "title": "Example",
        "error": "",
        "status_code": 200,
        "source": "crawl4ai",
    }

    response = await client.post("/fetch", json={"url": "https://example.com"})

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["source"] == "crawl4ai"
    assert "Example Domain" in data["markdown"]
    mock_fetch_chain.execute.assert_called_once_with("https://example.com")


@pytest.mark.anyio
async def test_fetch_failed_returns_200(client, mock_fetch_chain):
    """When execute() returns success=False the endpoint still returns 200, not 500."""
    mock_fetch_chain.execute.return_value = {
        "success": False,
        "url": "https://invalid.example",
        "markdown": "",
        "title": "",
        "error": "all tiers exhausted",
        "status_code": None,
        "source": "",
    }

    response = await client.post("/fetch", json={"url": "https://invalid.example"})

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False
    assert data["error"] == "all tiers exhausted"


@pytest.mark.anyio
async def test_fetch_format_query_param_accepted(client, mock_fetch_chain):
    """The ?format=html query param is accepted without error (router passes it through)."""
    mock_fetch_chain.execute.return_value = {
        "success": True,
        "url": "https://example.com",
        "markdown": "html content",
        "title": "Example",
        "error": "",
        "status_code": 200,
        "source": "crawl4ai",
    }

    response = await client.post("/fetch?format=html", json={"url": "https://example.com"})

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True


@pytest.mark.anyio
async def test_fetch_missing_url_returns_422(client, mock_fetch_chain):
    """POST /fetch with an empty body returns 422 validation error."""
    response = await client.post("/fetch", json={})

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any("url" in str(e) for e in errors)

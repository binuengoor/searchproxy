"""pytest tests for the POST /compat/firecrawl/scrape endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.main
import app.config
from app.main import app as fastapi_app
from app.routers.firecrawl import _get_fetch_chain
from app.services.crawl4ai import FetchResult


@pytest.fixture
def mock_fetch_chain():
    """Override the _get_fetch_chain dependency with a mock FetchChain."""
    chain = AsyncMock()
    fastapi_app.dependency_overrides[_get_fetch_chain] = lambda: chain
    yield chain
    fastapi_app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_firecrawl_scrape_success(client, mock_fetch_chain):
    """Happy path: returns Firecrawl-shaped JSON with success=true."""
    mock_fetch_chain.execute.return_value = FetchResult(
        success=True,
        url="https://example.com",
        markdown="# Example Domain\n\nThis domain is for use in illustrative examples.",
        title="Example Domain",
        description="An illustrative example domain",
        language="en",
        error="",
        status_code=200,
        source="crawl4ai",
    )

    response = await client.post("/compat/firecrawl/v2/scrape", json={"url": "https://example.com"})

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["data"]["markdown"] == "# Example Domain\n\nThis domain is for use in illustrative examples."
    assert data["data"]["metadata"]["title"] == "Example Domain"
    assert data["data"]["metadata"]["description"] == "An illustrative example domain"
    assert data["data"]["metadata"]["language"] == "en"
    assert data["data"]["metadata"]["sourceURL"] == "https://example.com"
    assert data["data"]["metadata"]["statusCode"] == 200
    assert data["data"]["html"] is None
    mock_fetch_chain.execute.assert_called_once_with("https://example.com/")


@pytest.mark.anyio
async def test_firecrawl_scrape_failure_returns_200(client, mock_fetch_chain):
    """Firecrawl contract: failures return HTTP 200 with success=false."""
    mock_fetch_chain.execute.return_value = FetchResult(
        success=False,
        url="https://cloudflare.example",
        markdown="",
        title="",
        error="all tiers exhausted",
        status_code=None,
        source="",
    )

    response = await client.post("/compat/firecrawl/v2/scrape", json={"url": "https://cloudflare.example"})

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False
    assert data["error"] == "all tiers exhausted"


@pytest.mark.anyio
async def test_firecrawl_scrape_ignores_unsupported_params(client, mock_fetch_chain):
    """Unsupported Firecrawl params are accepted and ignored."""
    mock_fetch_chain.execute.return_value = FetchResult(
        success=True,
        url="https://example.com",
        markdown="ok",
        title="Example",
        error="",
        status_code=200,
        source="crawl4ai",
    )

    payload = {
        "url": "https://example.com",
        "formats": ["markdown", "html"],
        "actions": [{"type": "click", "selector": "#btn"}],
        "location": {"country": "US"},
        "mobile": True,
        "waitFor": 2000,
    }
    response = await client.post("/compat/firecrawl/v2/scrape", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["data"]["markdown"] == "ok"


@pytest.mark.anyio
async def test_firecrawl_scrape_missing_url_returns_422(client, mock_fetch_chain):
    """Missing 'url' field returns 422 validation error."""
    response = await client.post("/compat/firecrawl/v2/scrape", json={})

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any("url" in str(e) for e in errors)


@pytest.mark.anyio
async def test_firecrawl_scrape_auth_required(auth_client, mock_fetch_chain):
    """When require_auth=true, the Bearer token is enforced via existing middleware."""
    mock_fetch_chain.execute.return_value = FetchResult(
        success=True,
        url="https://example.com",
        markdown="ok",
        title="Example",
        error="",
        status_code=200,
        source="crawl4ai",
    )

    response = await auth_client.post("/compat/firecrawl/v2/scrape", json={"url": "https://example.com"})

    assert response.status_code == 200
    assert response.json()["success"] is True


@pytest.mark.anyio
async def test_firecrawl_scrape_auth_rejected_without_token(client, mock_fetch_chain, monkeypatch):
    """When require_auth=true, missing token is rejected (401)."""
    # Patch app.main.settings because main.py does "from app.config import settings"
    # creating a local binding that does NOT update when we reassign app.config.settings.
    monkeypatch.setattr(
        app.main,
        "settings",
        app.config.Settings(SEARCHPROXY_REQUIRE_AUTH=True, SEARCHPROXY_API_KEY="real-key"),
    )

    response = await client.post("/compat/firecrawl/v2/scrape", json={"url": "https://example.com"})

    assert response.status_code == 401


@pytest.mark.anyio
async def test_firecrawl_scrape_empty_fields_map_to_none(client, mock_fetch_chain):
    """Empty description/language strings map to None in Firecrawl response."""
    mock_fetch_chain.execute.return_value = FetchResult(
        success=True,
        url="https://example.com",
        markdown="content",
        title="Title",
        description="",
        language="",
        status_code=200,
        source="jina",
    )

    response = await client.post("/compat/firecrawl/v2/scrape", json={"url": "https://example.com"})

    assert response.status_code == 200
    meta = response.json()["data"]["metadata"]
    assert meta["description"] is None
    assert meta["language"] is None

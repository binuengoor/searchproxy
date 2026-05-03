from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.main import app as fastapi_app
from app.dependencies import get_searxng_service
from app.services.searxng_compat import SearxngResponse, SearxngResult


def _mock_svc(results: list, query: str = "python") -> MagicMock:
    svc = MagicMock()
    svc.search = AsyncMock(
        return_value=SearxngResponse(
            query=query,
            number_of_results=len(results),
            results=results,
        )
    )
    return svc


@pytest.fixture
async def client(monkeypatch):
    """httpx AsyncClient against the FastAPI app."""
    import httpx

    _real_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    monkeypatch.setattr("app.main._client", _real_client)

    from httpx import ASGITransport

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fastapi_app), base_url="http://test"
    ) as ac:
        yield ac

    fastapi_app.dependency_overrides.clear()
    await _real_client.aclose()


@pytest.mark.anyio
async def test_general_search_returns_results(client):
    """GET /compat/searxng?q=python returns a populated SearxngResponse."""
    mock_results = [
        SearxngResult(title="Python Programming Language", url="https://python.org"),
        SearxngResult(title="Python Tutorial", url="https://example.com/python"),
    ]
    mock_svc = _mock_svc(mock_results, query="python")
    fastapi_app.dependency_overrides[get_searxng_service] = lambda: mock_svc

    response = await client.get("/compat/searxng", params={"q": "python"})

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "python"
    assert len(data["results"]) == 2
    assert data["results"][0]["title"] == "Python Programming Language"


@pytest.mark.anyio
async def test_images_passthrough_returns_results(client):
    """GET /compat/searxng?q=logo&categories=images returns image results."""
    mock_results = [
        SearxngResult(
            title="Python Logo",
            url="https://example.com/logo.png",
            category="images",
        ),
    ]
    mock_svc = _mock_svc(mock_results, query="logo")
    fastapi_app.dependency_overrides[get_searxng_service] = lambda: mock_svc

    response = await client.get(
        "/compat/searxng",
        params={"q": "logo", "categories": "images"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "logo"
    assert len(data["results"]) == 1
    assert data["results"][0]["category"] == "images"


@pytest.mark.anyio
async def test_empty_results(client):
    """When the service returns an empty list, the response has results=[]."""
    mock_svc = _mock_svc([], query="nonexistentquery123")
    fastapi_app.dependency_overrides[get_searxng_service] = lambda: mock_svc

    response = await client.get("/compat/searxng", params={"q": "nonexistentquery123"})

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "nonexistentquery123"
    assert data["number_of_results"] == 0
    assert data["results"] == []


@pytest.mark.anyio
async def test_missing_q_returns_422(client):
    """Omitting the required `q` parameter returns a 422 validation error."""
    response = await client.get("/compat/searxng")
    assert response.status_code == 422


@pytest.mark.anyio
async def test_videos_passthrough(client):
    """GET /compat/searxng?q=cat&categories=videos returns video results."""
    mock_results = [
        SearxngResult(
            title="Cat Video",
            url="https://example.com/cat.mp4",
            category="videos",
        ),
    ]
    mock_svc = _mock_svc(mock_results, query="cat")
    fastapi_app.dependency_overrides[get_searxng_service] = lambda: mock_svc

    response = await client.get(
        "/compat/searxng",
        params={"q": "cat", "categories": "videos"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["results"][0]["category"] == "videos"


@pytest.mark.anyio
async def test_optional_params_accepted(client):
    """Extra query parameters are forwarded to the service."""
    mock_svc = _mock_svc([], query="python")
    fastapi_app.dependency_overrides[get_searxng_service] = lambda: mock_svc

    response = await client.get(
        "/compat/searxng",
        params={
            "q": "python",
            "language": "en",
            "pageno": 2,
            "time_range": "year",
            "safesearch": 1,
            "autocomplete": "google",
        },
    )

    assert response.status_code == 200
    # Verify the service was called with the correct params
    call_args = mock_svc.search.await_args
    assert call_args is not None
    params = call_args[0][0]
    assert params.q == "python"
    assert params.language == "en"
    assert params.pageno == 2
    assert params.time_range == "year"
    assert params.safesearch == 1
    assert params.autocomplete == "google"

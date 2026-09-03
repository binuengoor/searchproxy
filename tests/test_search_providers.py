"""Tests for individual search providers."""

from __future__ import annotations

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.config import Settings
from app.services.search.providers.brave import BraveSearchProvider
from app.services.search.providers.exa import ExaSearchProvider
from app.services.search.providers.searxng import SearxngSearchProvider
from app.services.search.providers.serper import SerperSearchProvider
from app.services.search.providers.tavily import TavilySearchProvider


@pytest.fixture
def base_settings():
    return Settings(
        TAVILY_API_KEY="test-tavily-key",
        BRAVE_API_KEY="test-brave-key",
        EXA_API_KEY="test-exa-key",
        SERPER_API_KEY="test-serper-key",
        SEARXNG_URL="http://searxng:8080/search",
    )


@pytest.mark.asyncio
async def test_tavily_search_success(base_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [
            {
                "title": "Tavily Title",
                "url": "https://example.com/tavily",
                "content": "Tavily Content Snippet",
            }
        ]
    }
    client.post = AsyncMock(return_value=mock_resp)

    provider = TavilySearchProvider(client=client, settings=base_settings)
    assert provider.name == "tavily"
    assert provider.is_available is True
    assert provider.tier == 1

    results = await provider.search("python programming", max_results=5)
    assert len(results) == 1
    assert results[0].title == "Tavily Title"
    assert results[0].url == "https://example.com/tavily"
    assert results[0].snippet == "Tavily Content Snippet"

    # Verify authorization header and payload
    call_kwargs = client.post.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "Bearer test-tavily-key"
    assert call_kwargs["json"]["query"] == "python programming"
    assert call_kwargs["json"]["max_results"] == 5


@pytest.mark.asyncio
async def test_brave_search_success(base_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "web": {
            "results": [
                {
                    "title": "Brave Title",
                    "url": "https://example.com/brave",
                    "description": "Brave Description Snippet",
                }
            ]
        }
    }
    client.get = AsyncMock(return_value=mock_resp)

    provider = BraveSearchProvider(client=client, settings=base_settings)
    assert provider.name == "brave"
    assert provider.is_available is True
    assert provider.tier == 1

    results = await provider.search("fastapi tutorial", max_results=10)
    assert len(results) == 1
    assert results[0].title == "Brave Title"
    assert results[0].url == "https://example.com/brave"
    assert results[0].snippet == "Brave Description Snippet"

    call_kwargs = client.get.call_args.kwargs
    assert call_kwargs["headers"]["X-Subscription-Token"] == "test-brave-key"
    assert call_kwargs["params"]["q"] == "fastapi tutorial"


@pytest.mark.asyncio
async def test_exa_search_success(base_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [
            {
                "title": "Exa Title",
                "url": "https://example.com/exa",
                "text": "Exa Text Snippet",
            }
        ]
    }
    client.post = AsyncMock(return_value=mock_resp)

    provider = ExaSearchProvider(client=client, settings=base_settings)
    assert provider.name == "exa"
    assert provider.is_available is True
    assert provider.tier == 1

    results = await provider.search("neural networks", max_results=3)
    assert len(results) == 1
    assert results[0].title == "Exa Title"
    assert results[0].url == "https://example.com/exa"
    assert results[0].snippet == "Exa Text Snippet"

    call_kwargs = client.post.call_args.kwargs
    assert call_kwargs["headers"]["x-api-key"] == "test-exa-key"
    assert call_kwargs["json"]["query"] == "neural networks"
    assert call_kwargs["json"]["numResults"] == 3


@pytest.mark.asyncio
async def test_serper_search_success(base_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "organic": [
            {
                "title": "Google SERP Title",
                "link": "https://example.com/serper",
                "snippet": "Google SERP Snippet",
            }
        ]
    }
    client.post = AsyncMock(return_value=mock_resp)

    provider = SerperSearchProvider(client=client, settings=base_settings)
    assert provider.name == "serper"
    assert provider.is_available is True
    assert provider.tier == 1

    results = await provider.search("machine learning", max_results=5)
    assert len(results) == 1
    assert results[0].title == "Google SERP Title"
    assert results[0].url == "https://example.com/serper"
    assert results[0].snippet == "Google SERP Snippet"

    call_kwargs = client.post.call_args.kwargs
    assert call_kwargs["headers"]["X-API-KEY"] == "test-serper-key"
    assert call_kwargs["json"]["q"] == "machine learning"


@pytest.mark.asyncio
async def test_searxng_search_success(base_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [
            {
                "title": "SearXNG Title",
                "url": "https://example.com/searxng",
                "content": "SearXNG Content Snippet",
            }
        ]
    }
    client.get = AsyncMock(return_value=mock_resp)

    provider = SearxngSearchProvider(client=client, settings=base_settings)
    assert provider.name == "searxng"
    assert provider.is_available is True
    assert provider.tier == 2

    results = await provider.search("open source", max_results=5)
    assert len(results) == 1
    assert results[0].title == "SearXNG Title"
    assert results[0].url == "https://example.com/searxng"
    assert results[0].snippet == "SearXNG Content Snippet"

    call_kwargs = client.get.call_args.kwargs
    assert call_kwargs["params"]["q"] == "open source"
    assert call_kwargs["params"]["format"] == "json"


def test_provider_availability():
    empty_settings = Settings(
        TAVILY_API_KEY=None,
        BRAVE_API_KEY=None,
        EXA_API_KEY=None,
        SERPER_API_KEY=None,
        SEARXNG_URL=None,
    )
    client = MagicMock(spec=httpx.AsyncClient)
    assert TavilySearchProvider(client, empty_settings).is_available is False
    assert BraveSearchProvider(client, empty_settings).is_available is False
    assert ExaSearchProvider(client, empty_settings).is_available is False
    assert SerperSearchProvider(client, empty_settings).is_available is False
    assert SearxngSearchProvider(client, empty_settings).is_available is False

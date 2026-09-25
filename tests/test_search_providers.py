"""Tests for individual search providers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

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


@pytest.mark.asyncio
async def test_tavily_search_domains_and_freshness(base_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"results": []}
    client.post = AsyncMock(return_value=mock_resp)

    provider = TavilySearchProvider(client=client, settings=base_settings)
    await provider.search(
        "quantum computing",
        include_domains=["nature.com", "science.org"],
        exclude_domains=["quora.com"],
        freshness="month",
    )
    call_kwargs = client.post.call_args.kwargs
    payload = call_kwargs["json"]
    assert payload["include_domains"] == ["nature.com", "science.org"]
    assert payload["exclude_domains"] == ["quora.com"]
    assert payload["time_range"] == "month"


@pytest.mark.asyncio
async def test_brave_search_domains_and_freshness(base_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"web": {"results": []}}
    client.get = AsyncMock(return_value=mock_resp)

    provider = BraveSearchProvider(client=client, settings=base_settings)
    await provider.search(
        "fastapi tutorial",
        include_domains=["python.org", "github.com"],
        exclude_domains=["w3schools.com"],
        freshness="week",
    )
    call_kwargs = client.get.call_args.kwargs
    params = call_kwargs["params"]
    assert "(site:python.org OR site:github.com)" in params["q"]
    assert "-site:w3schools.com" in params["q"]
    assert params["freshness"] == "pw"


@pytest.mark.asyncio
async def test_brave_search_single_domain(base_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"web": {"results": []}}
    client.get = AsyncMock(return_value=mock_resp)

    provider = BraveSearchProvider(client=client, settings=base_settings)
    await provider.search(
        "docs",
        include_domains=["python.org"],
        freshness="day",
    )
    call_kwargs = client.get.call_args.kwargs
    params = call_kwargs["params"]
    assert "site:python.org" in params["q"]
    assert "OR" not in params["q"]
    assert params["freshness"] == "pd"


@pytest.mark.asyncio
async def test_exa_search_domains_and_freshness(base_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"results": []}
    client.post = AsyncMock(return_value=mock_resp)

    provider = ExaSearchProvider(client=client, settings=base_settings)
    await provider.search(
        "deep learning",
        include_domains=["arxiv.org"],
        exclude_domains=["medium.com"],
        freshness="day",
    )
    call_kwargs = client.post.call_args.kwargs
    payload = call_kwargs["json"]
    assert payload["includeDomains"] == ["arxiv.org"]
    assert payload["excludeDomains"] == ["medium.com"]
    assert "startPublishedDate" in payload
    assert payload["startPublishedDate"].endswith("Z")


@pytest.mark.asyncio
async def test_serper_search_domains_and_freshness(base_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"organic": []}
    client.post = AsyncMock(return_value=mock_resp)

    provider = SerperSearchProvider(client=client, settings=base_settings)
    await provider.search(
        "machine learning",
        include_domains=["reddit.com"],
        exclude_domains=["quora.com"],
        freshness="year",
    )
    call_kwargs = client.post.call_args.kwargs
    payload = call_kwargs["json"]
    assert "site:reddit.com" in payload["q"]
    assert "-site:quora.com" in payload["q"]
    assert payload["tbs"] == "qdr:y"


@pytest.mark.asyncio
async def test_searxng_search_domains_and_freshness(base_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"results": []}
    client.get = AsyncMock(return_value=mock_resp)

    provider = SearxngSearchProvider(client=client, settings=base_settings)
    await provider.search(
        "linux kernel",
        include_domains=["kernel.org"],
        exclude_domains=["spam.com"],
        freshness="day",
    )
    call_kwargs = client.get.call_args.kwargs
    params = call_kwargs["params"]
    assert "site:kernel.org" in params["q"]
    assert "-site:spam.com" in params["q"]
    assert params["time_range"] == "day"


@pytest.mark.asyncio
async def test_providers_normalize_url_domains(base_settings):
    client = MagicMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"results": [], "web": {"results": []}, "organic": []}
    client.get = AsyncMock(return_value=mock_resp)
    client.post = AsyncMock(return_value=mock_resp)

    # Brave
    brave = BraveSearchProvider(client=client, settings=base_settings)
    await brave.search(
        "query",
        include_domains=["https://docs.python.org/3/", "http://github.com:8080/"],
        exclude_domains=["https://spam.com/path"],
        freshness="24h",
    )
    brave_params = client.get.call_args.kwargs["params"]
    assert "(site:docs.python.org OR site:github.com)" in brave_params["q"]
    assert "-site:spam.com" in brave_params["q"]
    assert brave_params["freshness"] == "pd"

    # Tavily
    tavily = TavilySearchProvider(client=client, settings=base_settings)
    await tavily.search(
        "query",
        include_domains=["https://docs.python.org/"],
        exclude_domains=["https://spam.com/"],
        freshness="today",
    )
    tavily_payload = client.post.call_args.kwargs["json"]
    assert tavily_payload["include_domains"] == ["docs.python.org"]
    assert tavily_payload["exclude_domains"] == ["spam.com"]
    assert tavily_payload["time_range"] == "day"

    # Exa
    exa = ExaSearchProvider(client=client, settings=base_settings)
    await exa.search(
        "query",
        include_domains=["https://arxiv.org/abs/1234"],
        exclude_domains=["medium.com/"],
        freshness="pw",
    )
    exa_payload = client.post.call_args.kwargs["json"]
    assert exa_payload["includeDomains"] == ["arxiv.org"]
    assert exa_payload["excludeDomains"] == ["medium.com"]
    assert "startPublishedDate" in exa_payload

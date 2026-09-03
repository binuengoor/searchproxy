"""Tests for SearchRouter orchestration, rotation, circuit breaking, and fallbacks."""

from __future__ import annotations

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.config import Settings
from app.services.search.base import BaseSearchProvider
from app.services.search.models import SearchResult
from app.services.search.router import SearchRouter


class MockProvider(BaseSearchProvider):
    def __init__(self, name: str, tier: int, available: bool = True):
        self._name = name
        self._tier = tier
        self._available = available
        self.call_count = 0
        self.return_results: list[SearchResult] = [
            SearchResult(title=f"{name} Title", url=f"https://example.com/{name}", snippet=f"{name} Snippet")
        ]
        self.side_effect: Exception | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> int:
        return self._tier

    @property
    def is_available(self) -> bool:
        return self._available

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        self.call_count += 1
        if self.side_effect is not None:
            raise self.side_effect
        return self.return_results


@pytest.fixture
def test_settings():
    return Settings(
        SEARCH_COOLDOWN_SECONDS=60,
    )


@pytest.mark.asyncio
async def test_search_router_round_robin(test_settings):
    p1 = MockProvider("p1", tier=1)
    p2 = MockProvider("p2", tier=1)
    p3_fallback = MockProvider("searxng", tier=2)

    router = SearchRouter(
        client=MagicMock(),
        settings=test_settings,
        custom_providers=[p1, p2, p3_fallback],
    )

    # First search should hit p1
    res1 = await router.search("test query 1")
    assert res1.results[0].title == "p1 Title"
    assert p1.call_count == 1
    assert p2.call_count == 0
    assert p3_fallback.call_count == 0

    # Second search should hit p2 (round-robin)
    res2 = await router.search("test query 2")
    assert res2.results[0].title == "p2 Title"
    assert p1.call_count == 1
    assert p2.call_count == 1
    assert p3_fallback.call_count == 0

    # Third search wraps back to p1
    res3 = await router.search("test query 3")
    assert res3.results[0].title == "p1 Title"
    assert p1.call_count == 2
    assert p2.call_count == 1
    assert p3_fallback.call_count == 0


@pytest.mark.asyncio
async def test_search_router_429_circuit_breaker_failover(test_settings):
    p1 = MockProvider("p1", tier=1)
    # Simulate HTTP 429 error on p1
    mock_request = httpx.Request("POST", "https://api.example.com")
    mock_response = httpx.Response(status_code=429, request=mock_request)
    p1.side_effect = httpx.HTTPStatusError("Rate limited", request=mock_request, response=mock_response)

    p2 = MockProvider("p2", tier=1)
    fallback = MockProvider("searxng", tier=2)

    router = SearchRouter(
        client=MagicMock(),
        settings=test_settings,
        custom_providers=[p1, p2, fallback],
    )

    # Search should attempt p1, fail with 429, place p1 on cooldown, and succeed via p2
    res = await router.search("test query")
    assert res.results[0].title == "p2 Title"
    assert p1.call_count == 1
    assert p2.call_count == 1
    assert fallback.call_count == 0
    assert router._is_cooling_down("p1") is True

    # Next search should skip p1 (cooldown) and go straight to p2
    res2 = await router.search("test query 2")
    assert res2.results[0].title == "p2 Title"
    assert p1.call_count == 1  # Not called again
    assert p2.call_count == 2


@pytest.mark.asyncio
async def test_search_router_fallback_to_searxng(test_settings):
    p1 = MockProvider("p1", tier=1)
    p1.side_effect = RuntimeError("Network error")
    p2 = MockProvider("p2", tier=1)
    p2.side_effect = RuntimeError("Service unavailable")

    searxng = MockProvider("searxng", tier=2)

    router = SearchRouter(
        client=MagicMock(),
        settings=test_settings,
        custom_providers=[p1, p2, searxng],
    )

    # All Tier 1 fail -> Tier 2 (searxng) is called
    res = await router.search("test query")
    assert res.results[0].title == "searxng Title"
    assert p1.call_count == 1
    assert p2.call_count == 1
    assert searxng.call_count == 1


@pytest.mark.asyncio
async def test_search_router_graceful_degradation_when_all_fail(test_settings):
    p1 = MockProvider("p1", tier=1)
    p1.side_effect = RuntimeError("Down")
    searxng = MockProvider("searxng", tier=2)
    searxng.side_effect = RuntimeError("Down")

    router = SearchRouter(
        client=MagicMock(),
        settings=test_settings,
        custom_providers=[p1, searxng],
    )

    res = await router.search("test query")
    assert res.results == []


@pytest.mark.asyncio
async def test_search_router_cache_integration(test_settings):
    p1 = MockProvider("p1", tier=1)
    cache_mock = MagicMock()
    cache_mock.get_search = AsyncMock(return_value={"results": [{"title": "Cached Title", "url": "https://cached.com", "snippet": "Cached"}]})
    cache_mock.set_search = AsyncMock()

    router = SearchRouter(
        client=MagicMock(),
        settings=test_settings,
        cache=cache_mock,
        custom_providers=[p1],
    )

    # Should return cached results without calling provider
    res = await router.search("cached query")
    assert res.results[0].title == "Cached Title"
    assert p1.call_count == 0
    cache_mock.get_search.assert_awaited_once_with("cached query", 10)

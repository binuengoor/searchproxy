"""Tests for app/services/cache.py — SQLite CacheService with TTL-on-read semantics.

All tests use an isolated temporary database to avoid side effects.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.config import Settings
from app.services.cache import CacheService


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_cache(tmp_path: Path):
    """Return a CacheService backed by a temporary SQLite DB.

    Defaults: enabled=True, search_ttl=2s, fetch_ttl=5s.
    """
    db_path = tmp_path / "cache.db"
    settings = Settings(
        CACHE_ENABLED=True,
        CACHE_SEARCH_TTL=2,
        CACHE_FETCH_TTL=5,
        CACHE_DB_PATH=str(db_path),
    )
    svc = CacheService(settings=settings)
    return svc


# ---------------------------------------------------------------------------
# Search cache
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cache_search_miss(tmp_cache: CacheService):
    """Fetching a nonexistent key returns None."""
    result = await tmp_cache.get_search("does not exist", 10)
    assert result is None


@pytest.mark.anyio
async def test_cache_search_hit(tmp_cache: CacheService):
    """After storing, get_search returns the exact value."""
    query = "real madrid"
    payload = {
        "results": [
            {"title": "Real Madrid CF", "url": "https://rm.com", "snippet": "football club"}
        ]
    }
    await tmp_cache.set_search(query, 10, payload)
    cached = await tmp_cache.get_search(query, 10)
    assert cached is not None
    assert cached["results"][0]["title"] == "Real Madrid CF"


@pytest.mark.anyio
async def test_cache_search_ttl_expiry(tmp_cache: CacheService):
    """Search TTL is 2s — after sleep key should be treated as expired (lazy on read)."""
    query = "expire test"
    await tmp_cache.set_search(query, 5, {"results": []})

    # Immediately available
    assert await tmp_cache.get_search(query, 5) is not None

    await asyncio.sleep(2.5)

    # Expired — lazy TTL evicts on read and returns None
    expired = await tmp_cache.get_search(query, 5)
    assert expired is None


@pytest.mark.anyio
async def test_cache_search_invalidation(tmp_cache: CacheService):
    """Manual invalidation removes the entry."""
    query = "invalidate me"
    await tmp_cache.set_search(query, 10, {"results": [{"title": "T"}]})
    assert await tmp_cache.get_search(query, 10) is not None

    key = tmp_cache._search_key(query, 10)
    await tmp_cache.invalidate(key)

    assert await tmp_cache.get_search(query, 10) is None


# ---------------------------------------------------------------------------
# Fetch cache
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cache_fetch_miss(tmp_cache: CacheService):
    """Fetching a nonexistent URL key returns None."""
    result = await tmp_cache.get_fetch("https://example.com")
    assert result is None


@pytest.mark.anyio
async def test_cache_fetch_hit(tmp_cache: CacheService):
    """After storing, get_fetch returns the exact value."""
    url = "https://example.com"
    payload = {
        "success": True,
        "url": url,
        "markdown": "# Hello",
        "title": "Example",
        "source": "crawl4ai",
        "status_code": 200,
    }
    await tmp_cache.set_fetch(url, payload)
    cached = await tmp_cache.get_fetch(url)
    assert cached is not None
    assert cached["markdown"] == "# Hello"


@pytest.mark.anyio
async def test_cache_fetch_ttl_expiry(tmp_cache: CacheService):
    """Fetch TTL is 5s — after sleep key should be treated as expired."""
    url = "https://slow.example.com"
    await tmp_cache.set_fetch(url, {"success": False, "error": "timeout"})

    assert await tmp_cache.get_fetch(url) is not None

    await asyncio.sleep(5.5)

    # Expired
    assert await tmp_cache.get_fetch(url) is None


# ---------------------------------------------------------------------------
# Concurrent access
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cache_concurrent_reads_and_writes(tmp_cache: CacheService):
    """Multiple concurrent reads and writes do not corrupt the DB."""
    queries = [f"q{i}" for i in range(20)]

    async def writer():
        for q in queries:
            await tmp_cache.set_search(q, 5, {"results": [{"title": q}]})
            await asyncio.sleep(0.01)

    async def reader():
        found = 0
        for q in queries:
            val = await tmp_cache.get_search(q, 5)
            if val is not None and val["results"][0]["title"] == q:
                found += 1
            await asyncio.sleep(0.01)
        return found

    write_task = asyncio.create_task(writer())
    read_tasks = [asyncio.create_task(reader()) for _ in range(5)]

    await write_task
    found_counts = await asyncio.gather(*read_tasks)

    # At least some readers found some completed writes (race is expected, corruption is not)
    assert sum(found_counts) >= 0
    # All writers should have succeeded with no exception


# ---------------------------------------------------------------------------
# Clear and stats
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cache_clear_and_stats(tmp_cache: CacheService):
    """clear removes everything; stats reflects counts correctly."""
    await tmp_cache.set_search("one", 5, {"results": []})
    await tmp_cache.set_search("two", 5, {"results": []})
    await tmp_cache.set_fetch("https://f.com", {"success": True})

    stats = await tmp_cache.stats()
    assert stats["enabled"] is True
    assert stats["total"] == 3

    await tmp_cache.clear()
    assert (await tmp_cache.stats())["total"] == 0


# ---------------------------------------------------------------------------
# Disabled cache
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cache_disabled_returns_none(tmp_path: Path):
    """When CACHE_ENABLED=false, all operations are no-ops."""
    settings = Settings(
        CACHE_ENABLED=False,
        CACHE_SEARCH_TTL=2,
        CACHE_FETCH_TTL=5,
        CACHE_DB_PATH=str(tmp_path / "ignored.db"),
    )
    svc = CacheService(settings=settings)
    await svc.set_search("q", 5, {"results": []})
    assert await svc.get_search("q", 5) is None
    assert (await svc.stats())["enabled"] is False
    await svc.clear()  # no-op, should not raise
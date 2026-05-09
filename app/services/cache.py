"""SQLite caching layer for search results and fetch results.

Optional. If CACHE_ENABLED is false, all operations are no-ops.
TTL is enforced lazily on read — no background purging needed.
Survives container restarts via Docker volume mount (same as observability.db).

Inspired by app/observability.py — same SQLite-in-container pattern.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)


class CacheService:
    """Persistent key-value cache with TTL.

    Safe for async usage: all blocking SQLite calls run in the default
    event loop's thread executor.
    """

    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.CACHE_ENABLED
        self._db_path = Path(settings.CACHE_DB_PATH)
        self._search_ttl = settings.CACHE_SEARCH_TTL
        self._fetch_ttl = settings.CACHE_FETCH_TTL

        if self._enabled:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()
            log.info("Cache enabled: %s (search_ttl=%ds, fetch_ttl=%ds)", self._db_path, self._search_ttl, self._fetch_ttl)
        else:
            log.info("Cache disabled")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_search(self, query: str, max_results: int) -> Any | None:
        """Get cached search result (SearchResponse JSON)."""
        if not self._enabled:
            return None
        key = self._search_key(query, max_results)
        return await self._get(key)

    async def set_search(self, query: str, max_results: int, value: Any) -> None:
        """Cache a search result."""
        if not self._enabled:
            return
        key = self._search_key(query, max_results)
        await self._set(key, value, self._search_ttl)

    async def get_fetch(self, url: str) -> Any | None:
        """Get cached fetch result (FetchResult JSON)."""
        if not self._enabled:
            return None
        key = self._fetch_key(url)
        return await self._get(key)

    async def set_fetch(self, url: str, value: Any) -> None:
        """Cache a fetch result."""
        if not self._enabled:
            return
        key = self._fetch_key(url)
        await self._set(key, value, self._fetch_ttl)

    async def invalidate(self, key: str) -> None:
        """Remove a single key from cache."""
        if not self._enabled:
            return
        await self._run_in_executor(self._delete_sync, key)

    async def clear(self) -> None:
        """Remove all entries from cache."""
        if not self._enabled:
            return
        await self._run_in_executor(self._clear_sync)

    async def stats(self) -> dict[str, Any]:
        """Return cache stats (total entries, expired entries)."""
        if not self._enabled:
            return {"enabled": False, "total": 0, "expired": 0}
        return await self._run_in_executor(self._stats_sync)

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _search_key(query: str, max_results: int) -> str:
        """Normalize query and build a stable cache key."""
        normalized = " ".join(query.strip().lower().split())
        return f"search:{hash(normalized + f':{max_results}')}"

    @staticmethod
    def _fetch_key(url: str) -> str:
        return f"fetch:{hash(url.strip().lower())}"

    # ------------------------------------------------------------------
    # Core operations (run in executor for async safety)
    # ------------------------------------------------------------------

    async def _get(self, key: str) -> Any | None:
        return await self._run_in_executor(self._get_sync, key)

    async def _set(self, key: str, value: Any, ttl: int) -> None:
        await self._run_in_executor(self._set_sync, key, value, ttl)

    @staticmethod
    async def _run_in_executor(fn, *args):  # type: ignore[no-untyped-def]
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, fn, *args)

    # ------------------------------------------------------------------
    # Synchronous SQLite internals
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at)")
            conn.commit()
        finally:
            conn.close()

    def _get_sync(self, key: str) -> Any | None:
        import time

        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT value, expires_at FROM cache WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            value_json, expires_at = row
            if time.time() > expires_at:
                conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                conn.commit()
                return None
            return json.loads(value_json)
        finally:
            conn.close()

    def _set_sync(self, key: str, value: Any, ttl: int) -> None:
        import time

        conn = sqlite3.connect(self._db_path)
        try:
            expires_at = time.time() + ttl
            value_json = json.dumps(value, default=str)
            conn.execute(
                """
                INSERT INTO cache (key, value, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    expires_at = excluded.expires_at
                """,
                (key, value_json, expires_at),
            )
            conn.commit()
        finally:
            conn.close()

    def _delete_sync(self, key: str) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("DELETE FROM cache WHERE key = ?", (key,))
            conn.commit()
        finally:
            conn.close()

    def _clear_sync(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("DELETE FROM cache")
            conn.commit()
        finally:
            conn.close()

    def _stats_sync(self) -> dict[str, Any]:
        import time

        conn = sqlite3.connect(self._db_path)
        try:
            total = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            expired = conn.execute(
                "SELECT COUNT(*) FROM cache WHERE expires_at < ?",
                (time.time(),),
            ).fetchone()[0]
            return {"enabled": True, "total": total, "expired": expired}
        finally:
            conn.close()

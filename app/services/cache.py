"""SQLite caching layer for search, fetch, and rerank results.

Optional. If CACHE_ENABLED is false, all operations are no-ops.
TTL is enforced lazily on read — no background purging needed.
Survives container restarts via Docker volume mount (same as observability.db).

Uses persistent connections via threading.local to avoid open/close overhead
on every operation. Cache keys use hashlib.sha256 for deterministic hashing
across process restarts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.config import Settings
from app.services.sqlite_base import SQLiteBase

log = logging.getLogger(__name__)


class CacheService(SQLiteBase):
    """Persistent key-value cache with TTL.

    Safe for async usage: all blocking SQLite calls run via asyncio.to_thread.
    Uses a thread-local connection to avoid open/close overhead per operation.
    """

    _local = threading.local()

    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.CACHE_ENABLED
        self._db_path = str(Path(settings.CACHE_DB_PATH))
        self._search_ttl = settings.CACHE_SEARCH_TTL
        self._fetch_ttl = settings.CACHE_FETCH_TTL
        self._rerank_ttl = settings.CACHE_RERANK_TTL
        self._synthesis_ttl = settings.CACHE_SYNTHESIS_TTL

        if self._enabled:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            # Initialize schema on the main thread connection
            self._ensure_schema()
            log.info("Cache enabled: %s (search_ttl=%ds, fetch_ttl=%ds, rerank_ttl=%ds, synthesis_ttl=%ds)", self._db_path, self._search_ttl, self._fetch_ttl, self._rerank_ttl, self._synthesis_ttl)
        else:
            log.info("Cache disabled")

    # ------------------------------------------------------------------
    # Thread-local connection management
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local connection, creating and initializing one if needed."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            # Ensure schema exists on this connection's first use
            self._init_schema_on_conn(conn)
        return conn

    def _ensure_schema(self) -> None:
        """Initialize schema on the main thread connection."""
        conn = self._get_conn()
        # Schema init already done via _get_conn -> _init_schema_on_conn

    def _init_schema_on_conn(self, conn: sqlite3.Connection) -> None:
        """Create tables and indexes if they don't exist on the given connection."""
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
        except Exception:
            log.exception("Cache schema init failed")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_search(self, query: str, max_results: int) -> Any | None:
        """Get cached search result (SearchResponse JSON)."""
        if not self._enabled:
            return None
        key = self._search_key(query, max_results)
        return await asyncio.to_thread(self._get_sync, key)

    async def set_search(self, query: str, max_results: int, value: Any) -> None:
        """Cache a search result."""
        if not self._enabled:
            return
        key = self._search_key(query, max_results)
        await asyncio.to_thread(self._set_sync, key, value, self._search_ttl)

    async def get_fetch(self, url: str) -> Any | None:
        """Get cached fetch result (FetchResult JSON)."""
        if not self._enabled:
            return None
        key = self._fetch_key(url)
        return await asyncio.to_thread(self._get_sync, key)

    async def set_fetch(self, url: str, value: Any) -> None:
        """Cache a fetch result."""
        if not self._enabled:
            return
        key = self._fetch_key(url)
        await asyncio.to_thread(self._set_sync, key, value, self._fetch_ttl)

    async def get_rerank(self, query: str, documents: list[str]) -> Any | None:
        """Get cached rerank result (list of RerankResult-like dicts)."""
        if not self._enabled:
            return None
        key = self._rerank_key(query, documents)
        return await asyncio.to_thread(self._get_sync, key)

    async def set_rerank(self, query: str, documents: list[str], value: Any) -> None:
        """Cache a rerank result."""
        if not self._enabled:
            return
        key = self._rerank_key(query, documents)
        await asyncio.to_thread(self._set_sync, key, value, self._rerank_ttl)

    async def get_synthesize(self, query: str, sources: list[dict[str, Any]]) -> Any | None:
        """Get cached synthesis result (answer + citations JSON)."""
        if not self._enabled:
            return None
        key = self._synthesis_key(query, sources)
        return await asyncio.to_thread(self._get_sync, key)

    async def set_synthesize(self, query: str, sources: list[dict[str, Any]], value: Any) -> None:
        """Cache a synthesis result."""
        if not self._enabled:
            return
        key = self._synthesis_key(query, sources)
        await asyncio.to_thread(self._set_sync, key, value, self._synthesis_ttl)

    async def invalidate(self, key: str) -> None:
        """Remove a single key from cache."""
        if not self._enabled:
            return
        await asyncio.to_thread(self._delete_sync, key)

    async def clear(self) -> None:
        """Remove all entries from cache."""
        if not self._enabled:
            return
        await asyncio.to_thread(self._clear_sync)

    async def stats(self) -> dict[str, Any]:
        """Return cache stats (total entries, expired entries)."""
        if not self._enabled:
            return {"enabled": False, "total": 0, "expired": 0}
        return await asyncio.to_thread(self._stats_sync)

    # ------------------------------------------------------------------
    # Key helpers — deterministic hashing via hashlib.sha256
    # ------------------------------------------------------------------

    @staticmethod
    def _search_key(query: str, max_results: int) -> str:
        """Normalize query and build a stable cache key."""
        normalized = " ".join(query.strip().lower().split())
        digest = hashlib.sha256(f"{normalized}:{max_results}".encode()).hexdigest()[:16]
        return f"search:{digest}"

    @staticmethod
    def _fetch_key(url: str) -> str:
        digest = hashlib.sha256(url.strip().lower().encode()).hexdigest()[:16]
        return f"fetch:{digest}"

    @staticmethod
    def _rerank_key(query: str, documents: list[str]) -> str:
        """Build a deterministic cache key from query + document fingerprints.

        Uses SHA-256 of (query + concatenated doc hashes) so the same
        query with the same documents always produces the same key,
        even across process restarts.
        """
        normalized_query = " ".join(query.strip().lower().split())
        doc_fingerprints = "|".join(documents)
        digest = hashlib.sha256(f"rerank:{normalized_query}:{doc_fingerprints}".encode()).hexdigest()[:16]
        return f"rerank:{digest}"

    @staticmethod
    def _synthesis_key(query: str, sources: list[dict[str, Any]]) -> str:
        """Build a deterministic cache key from query + source URLs.

        The synthesis answer depends on the query and which sources were
        fetched. We key by query + sorted source URLs; fetch caching
        ensures the same URL returns consistent content within its TTL.
        """
        normalized_query = " ".join(query.strip().lower().split())
        source_fingerprint = "|".join(
            sorted(s.get("url", "") for s in sources)
        )
        digest = hashlib.sha256(
            f"synth:{normalized_query}:{source_fingerprint}".encode()
        ).hexdigest()[:16]
        return f"synth:{digest}"

    # ------------------------------------------------------------------
    # Synchronous SQLite internals (run via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _get_sync(self, key: str) -> Any | None:
        import time
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT value, expires_at FROM cache WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            value_json, expires_at = row
            if time.time() > expires_at:
                # Lazy expiry: don't delete on read miss to avoid write-lock
                # contention under load. Stale rows are harmless and tiny.
                return None
            return json.loads(value_json)
        except Exception:
            log.warning("Cache read failed for key %s", key, exc_info=True)
            return None

    def _set_sync(self, key: str, value: Any, ttl: int) -> None:
        import time

        def _write_cache(conn: sqlite3.Connection, key: str, value: Any, ttl: int) -> None:
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

        try:
            self._write(_write_cache, key, value, ttl)
        except sqlite3.OperationalError:
            log.warning("Cache write failed after retries for key %s", key, exc_info=True)
        except Exception:
            log.warning("Cache write failed for key %s", key, exc_info=True)

    def _delete_sync(self, key: str) -> None:
        def _delete_row(conn: sqlite3.Connection, key: str) -> None:
            conn.execute("DELETE FROM cache WHERE key = ?", (key,))

        try:
            self._write(_delete_row, key)
        except sqlite3.OperationalError:
            log.warning("Cache delete failed after retries for key %s", key, exc_info=True)
        except Exception:
            log.warning("Cache delete failed for key %s", key, exc_info=True)

    def _clear_sync(self) -> None:
        def _clear_all(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM cache")

        try:
            self._write(_clear_all)
        except sqlite3.OperationalError:
            log.warning("Cache clear failed after retries", exc_info=True)
        except Exception:
            log.warning("Cache clear failed", exc_info=True)

    def _stats_sync(self) -> dict[str, Any]:
        import time
        conn = self._get_conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            expired = conn.execute(
                "SELECT COUNT(*) FROM cache WHERE expires_at < ?",
                (time.time(),),
            ).fetchone()[0]
            return {"enabled": True, "total": total, "expired": expired}
        except Exception:
            return {"enabled": True, "total": 0, "expired": 0}

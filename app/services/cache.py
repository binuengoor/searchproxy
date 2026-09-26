"""SQLite caching layer for search, fetch, and rerank results.

Optional. If CACHE_ENABLED is false, all operations are no-ops.
TTL is enforced lazily on read — no background purging needed.
Survives container restarts via Docker volume mount (same as observability.db).

Uses persistent connections via threading.local to avoid open/close overhead
on every operation. Cache keys use hashlib.sha256 for deterministic hashing
across process restarts.

Supports semantic vector cache lookup via fastembed embeddings with sub-15ms
retrieval and cosine similarity matching >= threshold (default 0.92).
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
from app.services.search.base import normalize_domain, normalize_freshness
from app.services.sqlite_base import SQLiteBase

log = logging.getLogger(__name__)


class CacheService(SQLiteBase):
    """Persistent key-value and semantic vector cache with TTL.

    Safe for async usage: all blocking SQLite calls run via asyncio.to_thread.
    Uses a thread-local connection to avoid open/close overhead per operation.
    """

    def __init__(self, settings: Settings) -> None:
        db_path = getattr(settings, "CACHE_DB_PATH", None) or getattr(
            settings, "CACHE_SQLITE_PATH", "/data/cache.db"
        )
        super().__init__(str(Path(db_path)))
        self._settings = settings
        self._enabled = settings.CACHE_ENABLED
        self._search_ttl = settings.CACHE_SEARCH_TTL
        self._fetch_ttl = settings.CACHE_FETCH_TTL
        self._rerank_ttl = settings.CACHE_RERANK_TTL
        self._synthesis_ttl = settings.CACHE_SYNTHESIS_TTL

        self._semantic_enabled = getattr(settings, "CACHE_SEMANTIC_ENABLED", True)
        self._semantic_threshold = getattr(settings, "CACHE_SEMANTIC_THRESHOLD", 0.92)
        self._semantic_model_name = getattr(
            settings, "CACHE_SEMANTIC_MODEL", "BAAI/bge-small-en-v1.5"
        )
        self._embedding_model: Any = None
        self._embedding_lock = threading.Lock()
        self._embedding_failed = False

        if self._enabled:
            self._ensure_dirs()
            self._init_schema()
            log.info(
                "Cache enabled: %s (search_ttl=%ds, fetch_ttl=%ds, rerank_ttl=%ds, "
                "synthesis_ttl=%ds, semantic=%s)",
                self._db_path,
                self._search_ttl,
                self._fetch_ttl,
                self._rerank_ttl,
                self._synthesis_ttl,
                self._semantic_enabled,
            )
        else:
            log.info("Cache disabled")

    def _create_schema(self, conn: sqlite3.Connection) -> None:
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

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS semantic_cache (
                    key TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    params_hash TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    value TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sem_cache_exp "
                "ON semantic_cache(expires_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sem_cache_params "
                "ON semantic_cache(params_hash)"
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS research_dossiers (
                    id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    dossier TEXT NOT NULL,
                    format TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    metadata TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_created "
                "ON research_dossiers(created_at DESC)"
            )
        except Exception:
            log.exception("Cache schema init failed")

    # ------------------------------------------------------------------
    # Embedding helper for Semantic Vector Cache
    # ------------------------------------------------------------------

    def _get_embedding_sync(self, text: str) -> Any | None:
        """Compute float32 vector embedding for text using fastembed with lazy init.

        Gracefully falls back to None if model is busy (lock timeout) or failed.
        """
        if self._embedding_failed:
            return None

        acquired = self._embedding_lock.acquire(timeout=0.5)
        if not acquired:
            log.warning("Embedding model busy; falling back to exact matching for '%s'", text)
            return None

        try:
            if self._embedding_model is None and not self._embedding_failed:
                try:
                    import os

                    from fastembed import TextEmbedding

                    cache_dir = getattr(self._settings, "FASTEMBED_CACHE_PATH", None)
                    if cache_dir:
                        try:
                            os.makedirs(cache_dir, exist_ok=True)
                        except OSError:
                            cache_dir = "/tmp/fastembed"
                            os.makedirs(cache_dir, exist_ok=True)
                    self._embedding_model = TextEmbedding(
                        model_name=self._semantic_model_name,
                        cache_dir=cache_dir,
                    )
                except Exception as exc:
                    self._embedding_failed = True
                    log.warning("Fastembed init failed for semantic cache: %s", exc)
                    return None

            if self._embedding_model is None:
                return None

            try:
                import numpy as np

                emb_generator = self._embedding_model.embed([text])
                emb = next(iter(emb_generator))
                arr = np.asarray(emb, dtype=np.float32)
                norm = np.linalg.norm(arr)
                if norm > 0:
                    arr = arr / norm
                return arr
            except Exception as exc:
                log.warning("Embedding generation failed for text '%s': %s", text, exc)
                return None
        finally:
            self._embedding_lock.release()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_search(
        self,
        query: str,
        max_results: int,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        freshness: str | None = None,
        semantic: bool = True,
    ) -> Any | None:
        """Get cached search result (SearchResponse JSON).

        Checks exact key match first (sub-1ms). If exact key misses, attempts
        semantic vector cache lookup (cosine similarity >= threshold).
        """
        if not self._enabled:
            return None

        # 1. Exact key match
        key = self._search_key(query, max_results, include_domains, exclude_domains, freshness)
        exact = await asyncio.to_thread(self._get_sync, key)
        if exact is not None:
            return exact

        # 2. Semantic vector lookup
        if self._semantic_enabled and semantic:
            params_hash = self._search_params_hash(
                max_results, include_domains, exclude_domains, freshness
            )
            semantic_hit = await asyncio.to_thread(
                self._find_semantic_match_sync, query, params_hash
            )
            if semantic_hit is not None:
                return semantic_hit

        return None

    async def set_search(
        self,
        query: str,
        max_results: int,
        value: Any,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        freshness: str | None = None,
    ) -> None:
        """Cache a search result with both exact key and semantic vector representation."""
        if not self._enabled:
            return
        key = self._search_key(query, max_results, include_domains, exclude_domains, freshness)
        await asyncio.to_thread(self._set_sync, key, value, self._search_ttl)

        if self._semantic_enabled:
            params_hash = self._search_params_hash(
                max_results, include_domains, exclude_domains, freshness
            )
            await asyncio.to_thread(
                self._set_semantic_sync, key, query, params_hash, value, self._search_ttl
            )

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
        """Remove a single key from cache (both exact and semantic)."""
        if not self._enabled:
            return
        await asyncio.to_thread(self._delete_sync, key)

    async def clear(self) -> None:
        """Remove all entries from cache."""
        if not self._enabled:
            return
        await asyncio.to_thread(self._clear_sync)

    async def stats(self) -> dict[str, Any]:
        """Return cache stats (total entries, expired entries, semantic entries)."""
        if not self._enabled:
            return {"enabled": False, "total": 0, "expired": 0, "semantic": 0}
        return await asyncio.to_thread(self._stats_sync)

    # ------------------------------------------------------------------
    # Research Dossier Storage (MCP Resources)
    # ------------------------------------------------------------------

    async def save_research_dossier(
        self,
        dossier_id: str,
        query: str,
        summary: str,
        dossier: str,
        format_type: str = "markdown",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Store a completed research dossier for MCP resource reading."""
        if not self._enabled:
            return
        await asyncio.to_thread(
            self._save_research_sync,
            dossier_id,
            query,
            summary,
            dossier,
            format_type,
            metadata or {},
        )

    async def get_recent_research(self, limit: int = 10) -> list[dict[str, Any]]:
        """List recent deep research summaries."""
        if not self._enabled:
            return []
        return await asyncio.to_thread(self._get_recent_research_sync, limit)

    async def get_research_by_id(self, dossier_id: str) -> dict[str, Any] | None:
        """Retrieve full research dossier by identifier."""
        if not self._enabled:
            return None
        return await asyncio.to_thread(self._get_research_by_id_sync, dossier_id)

    # ------------------------------------------------------------------
    # Key helpers — deterministic hashing via hashlib.sha256
    # ------------------------------------------------------------------

    @staticmethod
    def _search_key(
        query: str,
        max_results: int,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        freshness: str | None = None,
    ) -> str:
        """Normalize query, domains, and freshness to build a stable cache key."""
        normalized = " ".join(query.strip().lower().split())
        inc_str = ",".join(
            sorted(
                dict.fromkeys(
                    normalize_domain(d) for d in (include_domains or []) if normalize_domain(d)
                )
            )
        )
        exc_str = ",".join(
            sorted(
                dict.fromkeys(
                    normalize_domain(d) for d in (exclude_domains or []) if normalize_domain(d)
                )
            )
        )
        fresh_str = normalize_freshness(freshness) or ""
        raw_key = f"{normalized}:{max_results}:{inc_str}:{exc_str}:{fresh_str}"
        digest = hashlib.sha256(raw_key.encode()).hexdigest()[:16]
        return f"search:{digest}"

    @staticmethod
    def _search_params_hash(
        max_results: int,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        freshness: str | None = None,
    ) -> str:
        """Compute parameter hash to ensure semantic cache only matches compatible queries."""
        inc_str = ",".join(
            sorted(
                dict.fromkeys(
                    normalize_domain(d) for d in (include_domains or []) if normalize_domain(d)
                )
            )
        )
        exc_str = ",".join(
            sorted(
                dict.fromkeys(
                    normalize_domain(d) for d in (exclude_domains or []) if normalize_domain(d)
                )
            )
        )
        fresh_str = normalize_freshness(freshness) or ""
        raw_params = f"{max_results}:{inc_str}:{exc_str}:{fresh_str}"
        return hashlib.sha256(raw_params.encode()).hexdigest()[:16]

    @staticmethod
    def _fetch_key(url: str) -> str:
        digest = hashlib.sha256(url.strip().lower().encode()).hexdigest()[:16]
        return f"fetch:{digest}"

    @staticmethod
    def _rerank_key(query: str, documents: list[str]) -> str:
        normalized_query = " ".join(query.strip().lower().split())
        doc_fingerprints = "|".join(documents)
        raw = f"rerank:{normalized_query}:{doc_fingerprints}"
        digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"rerank:{digest}"

    @staticmethod
    def _synthesis_key(query: str, sources: list[dict[str, Any]]) -> str:
        normalized_query = " ".join(query.strip().lower().split())
        source_fingerprint = "|".join(sorted(s.get("url", "") for s in sources))
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
                return None
            return json.loads(value_json)
        except Exception:
            log.warning("Cache read failed for key %s", key, exc_info=True)
            return None

    def _find_semantic_match_sync(self, query: str, params_hash: str) -> Any | None:
        """Find the closest matching query embedding with similarity >= threshold."""
        import time

        import numpy as np

        q_emb = self._get_embedding_sync(query)
        if q_emb is None:
            return None

        conn = self._get_conn()
        now = time.time()
        try:
            sql = (
                "SELECT query, embedding, value FROM semantic_cache "
                "WHERE params_hash = ? AND expires_at > ?"
            )
            rows = conn.execute(sql, (params_hash, now)).fetchall()
            if not rows:
                return None

            embeddings = []
            valid_indices = []
            for idx, r in enumerate(rows):
                try:
                    c_emb = np.frombuffer(r[1], dtype=np.float32)
                    c_norm = np.linalg.norm(c_emb)
                    if c_norm > 0:
                        c_emb = c_emb / c_norm
                    embeddings.append(c_emb)
                    valid_indices.append(idx)
                except Exception:
                    continue

            if not embeddings:
                return None

            mat = np.stack(embeddings)  # Shape: (N, D)
            sims = mat @ q_emb          # Shape: (N,)
            best_idx = int(np.argmax(sims))
            best_sim = float(sims[best_idx])

            if best_sim >= self._semantic_threshold:
                best_row = rows[valid_indices[best_idx]]
                best_query = best_row[0]
                best_value = best_row[2]
                log.info(
                    "Semantic cache HIT: '%s' matched '%s' (similarity=%.4f >= %.2f)",
                    query,
                    best_query,
                    best_sim,
                    self._semantic_threshold,
                )
                return json.loads(best_value)
            return None
        except Exception:
            log.warning("Semantic cache lookup failed for query '%s'", query, exc_info=True)
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

    def _set_semantic_sync(
        self,
        key: str,
        query: str,
        params_hash: str,
        value: Any,
        ttl: int,
    ) -> None:
        import time

        q_emb = self._get_embedding_sync(query)
        if q_emb is None:
            return

        emb_bytes = q_emb.tobytes()
        expires_at = time.time() + ttl
        value_json = json.dumps(value, default=str)

        def _write_semantic(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO semantic_cache (key, query, params_hash, embedding, value, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    query = excluded.query,
                    params_hash = excluded.params_hash,
                    embedding = excluded.embedding,
                    value = excluded.value,
                    expires_at = excluded.expires_at
                """,
                (key, query, params_hash, emb_bytes, value_json, expires_at),
            )

        try:
            self._write(_write_semantic)
        except sqlite3.OperationalError:
            log.warning("Semantic cache write failed after retries for key %s", key, exc_info=True)
        except Exception:
            log.warning("Semantic cache write failed for key %s", key, exc_info=True)

    def _delete_sync(self, key: str) -> None:
        def _delete_row(conn: sqlite3.Connection, key: str) -> None:
            conn.execute("DELETE FROM cache WHERE key = ?", (key,))
            conn.execute("DELETE FROM semantic_cache WHERE key = ?", (key,))

        try:
            self._write(_delete_row, key)
        except sqlite3.OperationalError:
            log.warning("Cache delete failed after retries for key %s", key, exc_info=True)
        except Exception:
            log.warning("Cache delete failed for key %s", key, exc_info=True)

    def _clear_sync(self) -> None:
        def _clear_all(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM cache")
            conn.execute("DELETE FROM semantic_cache")

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
            now = time.time()
            total = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            expired = conn.execute(
                "SELECT COUNT(*) FROM cache WHERE expires_at < ?",
                (now,),
            ).fetchone()[0]
            semantic_count = conn.execute(
                "SELECT COUNT(*) FROM semantic_cache WHERE expires_at > ?",
                (now,),
            ).fetchone()[0]
            return {
                "enabled": True,
                "total": total,
                "expired": expired,
                "semantic": semantic_count,
            }
        except Exception:
            return {"enabled": True, "total": 0, "expired": 0, "semantic": 0}

    def _save_research_sync(
        self,
        dossier_id: str,
        query: str,
        summary: str,
        dossier: str,
        format_type: str,
        metadata: dict[str, Any],
    ) -> None:
        import time

        created_at = time.time()
        meta_json = json.dumps(metadata, default=str)

        def _write_dossier(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO research_dossiers (
                    id, query, summary, dossier, format, created_at, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    query = excluded.query,
                    summary = excluded.summary,
                    dossier = excluded.dossier,
                    format = excluded.format,
                    created_at = excluded.created_at,
                    metadata = excluded.metadata
                """,
                (dossier_id, query, summary, dossier, format_type, created_at, meta_json),
            )

        try:
            self._write(_write_dossier)
        except Exception:
            log.warning("Failed to store research dossier %s", dossier_id, exc_info=True)

    def _get_recent_research_sync(self, limit: int) -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """
                SELECT id, query, summary, format, created_at, metadata
                FROM research_dossiers
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            results = []
            for r in rows:
                try:
                    meta = json.loads(r[5])
                except Exception:
                    meta = {}
                results.append(
                    {
                        "id": r[0],
                        "query": r[1],
                        "summary": r[2],
                        "format": r[3],
                        "created_at": r[4],
                        "citations_count": meta.get("citations", 0),
                        "sources_count": meta.get("sources", 0),
                        "metadata": meta,
                        "resource_uri": f"searchproxy://research/{r[0]}",
                    }
                )
            return results
        except Exception:
            log.warning("Failed to list recent research dossiers", exc_info=True)
            return []

    def _get_research_by_id_sync(self, dossier_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        try:
            row = conn.execute(
                """
                SELECT id, query, summary, dossier, format, created_at, metadata
                FROM research_dossiers
                WHERE id = ?
                """,
                (dossier_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                meta = json.loads(row[6])
            except Exception:
                meta = {}
            return {
                "id": row[0],
                "query": row[1],
                "summary": row[2],
                "dossier": row[3],
                "format": row[4],
                "created_at": row[5],
                "citations_count": meta.get("citations", 0),
                "sources_count": meta.get("sources", 0),
                "metadata": meta,
                "resource_uri": f"searchproxy://research/{row[0]}",
            }
        except Exception:
            log.warning("Failed to read research dossier %s", dossier_id, exc_info=True)
            return None

"""In-process SQLite observability store.

Optional. If OBSERVABILITY_ENABLED is false, all operations are no-ops.
No external containers, no network overhead, set-and-forget.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from app.config import Settings

log = logging.getLogger(__name__)

_MAX_BODY_BYTES = 8_192
_store: "ObservabilityStore | None" = None


def _truncate(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if len(text) > _MAX_BODY_BYTES:
        return text[:_MAX_BODY_BYTES] + "\n...[truncated]"
    return text


def _mask_headers(headers: dict[str, str]) -> dict[str, str]:
    masked = {}
    for k, v in headers.items():
        lower = k.lower()
        if lower in ("authorization", "x-api-key", "cookie") or any(
            s in lower for s in ("api-key", "api_key", "token", "secret")
        ):
            masked[k] = "***"
        else:
            masked[k] = v
    return masked


@dataclass(frozen=True, slots=True)
class LogRecord:
    request_id: str
    method: str
    path: str
    query_params: str
    status_code: int | None
    response_time_ms: float
    client_ip: str
    user_agent: str | None
    request_headers: dict[str, str]
    request_body: str
    response_headers: dict[str, str]
    response_body: str
    error: str | None
    source: str
    correlation_id: str = ""


class ObservabilityStore:
    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.OBSERVABILITY_ENABLED
        self._retention_days = settings.OBSERVABILITY_RETENTION_DAYS
        self._db_path = Path(settings.OBSERVABILITY_DB_PATH)
        if self._enabled:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()
            log.info("Observability enabled: %s", self._db_path)
        else:
            log.info("Observability disabled")

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    request_id TEXT NOT NULL,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    query_params TEXT,
                    status_code INTEGER,
                    response_time_ms REAL,
                    client_ip TEXT,
                    user_agent TEXT,
                    request_headers TEXT,
                    request_body TEXT,
                    response_headers TEXT,
                    response_body TEXT,
                    error TEXT,
                    source TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_timestamp ON request_logs(timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_path ON request_logs(path)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_status ON request_logs(status_code)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_source ON request_logs(source)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_id ON request_logs(request_id)"
            )
            # Migrate: add correlation_id column if missing (existing DBs)
            try:
                conn.execute("ALTER TABLE request_logs ADD COLUMN correlation_id TEXT")
                log.info("Added correlation_id column to request_logs")
            except sqlite3.OperationalError:
                pass  # Column already exists
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_correlation_id ON request_logs(correlation_id)"
            )

    async def insert(self, record: LogRecord) -> None:
        if not self._enabled:
            return
        await asyncio.to_thread(self._insert_sync, record)

    def _insert_sync(self, record: LogRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO request_logs (
                    timestamp, request_id, method, path, query_params,
                    status_code, response_time_ms, client_ip, user_agent,
                    request_headers, request_body, response_headers,
                    response_body, error, source, correlation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).timestamp(),
                    record.request_id,
                    record.method,
                    record.path,
                    record.query_params,
                    record.status_code,
                    round(record.response_time_ms, 2),
                    record.client_ip,
                    record.user_agent,
                    json.dumps(_mask_headers(record.request_headers)),
                    _truncate(record.request_body),
                    json.dumps(_mask_headers(record.response_headers)),
                    _truncate(record.response_body),
                    record.error or "",
                    record.source,
                    record.correlation_id,
                ),
            )

    async def purge_old(self) -> int:
        if not self._enabled or self._retention_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - (self._retention_days * 86400)
        return await asyncio.to_thread(self._purge_sync, cutoff)

    def _purge_sync(self, cutoff: float) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM request_logs WHERE timestamp < ?", (cutoff,)
            )
            deleted = cur.rowcount
            if deleted:
                log.info("Purged %d old observability records", deleted)
                conn.execute("VACUUM")
            return deleted

    async def delete_all(self) -> int:
        """Delete every record in the store. Returns number of rows removed."""
        if not self._enabled:
            return 0
        return await asyncio.to_thread(self._delete_all_sync)

    def _delete_all_sync(self) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM request_logs")
            deleted = cur.rowcount
            if deleted:
                log.info("Cleared all %d observability records", deleted)
                conn.execute("VACUUM")
            return deleted

    async def query(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        method: str | None = None,
        path: str | None = None,
        status_code: int | None = None,
        source: str | None = None,
        search: str | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        if not self._enabled:
            return [], 0
        return await asyncio.to_thread(
            self._query_sync,
            limit,
            offset,
            method,
            path,
            status_code,
            source,
            search,
            start_time,
            end_time,
        )

    def _query_sync(
        self,
        limit: int,
        offset: int,
        method: str | None,
        path: str | None,
        status_code: int | None,
        source: str | None,
        search: str | None,
        start_time: float | None,
        end_time: float | None,
    ) -> tuple[list[dict[str, Any]], int]:
        where = ["1=1"]
        params: list[Any] = []
        if method:
            where.append("method = ?")
            params.append(method.upper())
        if path:
            where.append("path LIKE ?")
            params.append(f"%{path}%")
        if status_code is not None:
            where.append("status_code = ?")
            params.append(status_code)
        if source:
            where.append("source = ?")
            params.append(source)
        if start_time:
            where.append("timestamp >= ?")
            params.append(start_time)
        if end_time:
            where.append("timestamp <= ?")
            params.append(end_time)
        if search:
            where.append(
                "("
                "request_id LIKE ? OR method LIKE ? OR path LIKE ? OR "
                "query_params LIKE ? OR request_body LIKE ? OR response_body LIKE ? OR "
                "error LIKE ? OR source LIKE ? OR user_agent LIKE ? OR "
                "CAST(status_code AS TEXT) LIKE ?"
                ")"
            )
            like = f"%{search}%"
            params.extend([like] * 10)

        where_clause = " AND ".join(where)

        with self._conn() as conn:
            count_cur = conn.execute(
                f"SELECT COUNT(*) FROM request_logs WHERE {where_clause}", params
            )
            total = count_cur.fetchone()[0]

            cur = conn.execute(
                f"""
                SELECT * FROM request_logs
                WHERE {where_clause}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            )
            rows = [dict(row) for row in cur.fetchall()]
            return rows, total


def get_store() -> ObservabilityStore | None:
    return _store


def init_store(settings: Settings) -> ObservabilityStore:
    global _store
    _store = ObservabilityStore(settings=settings)
    return _store
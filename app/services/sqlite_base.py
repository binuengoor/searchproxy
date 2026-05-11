"""SQLite base class for thread-local connection management.

Both CacheService and ObservabilityStore use the same pattern:
threading.local() for connection reuse, PRAGMA WAL + NORMAL on connect,
and per-connection schema init. This base class eliminates that duplication.

Concurrency: SQLite supports concurrent reads in WAL mode, but only one
writer at a time. We use a threading.Lock to serialize writes and
add retry logic for OperationalError("database is locked") on reads.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Default retry settings for write operations
_WRITE_RETRIES = 3
_WRITE_RETRY_DELAY = 0.05  # 50ms between retries


class SQLiteBase:
    """Base class providing thread-local SQLite connection management.

    Subclasses must:
    - Set ``_db_path`` in __init__
    - Implement ``_create_schema(conn)`` to set up tables
    - Call ``self._ensure_dirs()`` and ``self._init_schema()`` in __init__

    Thread safety:
    - Reads: concurrent via WAL mode (multiple readers don't block each other)
    - Writes: serialized via ``_write_lock`` to prevent "database is locked"
    - Use ``_write(func, *args)`` for any mutation (INSERT, UPDATE, DELETE)
      to get automatic retry on lock contention
    """

    _local = threading.local()
    _write_lock = threading.Lock()

    def _ensure_dirs(self) -> None:
        """Create parent directory for the database file if it doesn't exist."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local connection, creating and configuring one if needed."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            self._create_schema(conn)
        return conn

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        """Create tables and indexes. Subclasses must implement this."""
        raise NotImplementedError

    def _init_schema(self) -> None:
        """Initialize schema on the main thread connection."""
        self._get_conn()

    def _write(self, fn, *args, retries: int = _WRITE_RETRIES):
        """Execute a write operation with lock + retry to handle contention.

        Acquires the write lock, runs ``fn(conn, *args)``, and retries
        on ``OperationalError("database is locked")`` with exponential backoff.
        Commits on success.

        Args:
            fn: A callable ``(conn, *args) -> None`` that executes write SQL.
            *args: Extra arguments passed to fn (after conn).
            retries: Max retry attempts for lock contention.
        """
        with self._write_lock:
            conn = self._get_conn()
            for attempt in range(retries):
                try:
                    fn(conn, *args)
                    conn.commit()
                    return
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).lower() and attempt < retries - 1:
                        delay = _WRITE_RETRY_DELAY * (2 ** attempt)
                        log.debug(
                            "SQLite write lock contention (attempt %d/%d), retrying in %.0fms",
                            attempt + 1, retries, delay * 1000,
                        )
                        time.sleep(delay)
                        continue
                    raise
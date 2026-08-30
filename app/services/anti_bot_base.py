"""Base class for anti-bot firebreak services with monthly credit tracking."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote

import sqlite3
import httpx

from app.config import Settings
from app.services.fetch_utils import safe_fetch
from app.services.models import FetchResult
from app.services.sqlite_base import SQLiteBase

log = logging.getLogger(__name__)


def _current_month_key() -> int:
    """Return a comparable integer for the current calendar month.

    ``year * 12 + (month - 1)`` preserves ordering across year boundaries.
    """
    now = datetime.now(timezone.utc)
    return now.year * 12 + (now.month - 1)


def _check_credits(used: int, limit: int) -> bool:
    """Return True if credits are still available."""
    return used < limit


class QuotaStore(SQLiteBase):
    """SQLite-backed persistent quota storage for anti-bot credits."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self._ensure_dirs()
        self._init_schema()

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS monthly_credits (
                service_name TEXT NOT NULL,
                month_key INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (service_name, month_key)
            )
        """)

    def get_used(self, service_name: str, month_key: int) -> int:
        try:
            conn = self._get_conn()
            cur = conn.execute(
                "SELECT used FROM monthly_credits WHERE service_name = ? AND month_key = ?",
                (service_name, month_key),
            )
            row = cur.fetchone()
            return row[0] if row else 0
        except Exception as e:
            log.warning("QuotaStore get_used failed for %s: %s", service_name, e)
            return 0

    def increment(self, service_name: str, month_key: int) -> int:
        def _do_inc(conn: sqlite3.Connection) -> int:
            conn.execute("""
                INSERT INTO monthly_credits (service_name, month_key, used)
                VALUES (?, ?, 1)
                ON CONFLICT(service_name, month_key) DO UPDATE SET used = used + 1
            """, (service_name, month_key))
            cur = conn.execute(
                "SELECT used FROM monthly_credits WHERE service_name = ? AND month_key = ?",
                (service_name, month_key),
            )
            return cur.fetchone()[0]

        try:
            return self._write(_do_inc)
        except Exception as e:
            log.warning("QuotaStore increment failed for %s: %s", service_name, e)
            return 0


_global_quota_store: QuotaStore | None = None


def get_quota_store(settings: Settings) -> QuotaStore:
    """Get or create the singleton QuotaStore."""
    global _global_quota_store
    if _global_quota_store is None:
        _global_quota_store = QuotaStore(settings.CACHE_DB_PATH)
    return _global_quota_store


class AntiBotClient:
    """Base class for anti-bot fetch services with monthly credit tracking.

    Subclasses set ``_SERVICE_NAME``, ``_API_URL_TEMPLATE``, ``_SOURCE``,
    and ``_DEFAULT_CREDIT_LIMIT``, then ``fetch()`` works automatically.

    Graceful degradation: on any error (timeout, HTTP error, credit exhaustion)
    returns ``FetchResult(success=False, ...)`` so callers always get a valid
    result and never need to handle exceptions.
    """

    _SERVICE_NAME: str = ""       # e.g. "scrape_do", "scraperapi"
    _API_URL_TEMPLATE: str = ""  # e.g. "https://api.scrape.do/?token={key}&url={url}"
    _SOURCE: str = ""            # e.g. "scrape_do", "scraperapi"
    _DEFAULT_CREDIT_LIMIT: int = 1000

    def __init__(self, client: httpx.AsyncClient, settings: Settings, quota_store: QuotaStore | None = None) -> None:
        self._client = client
        self._settings = settings
        self._quota_store = quota_store or get_quota_store(settings)
        self._timeout = httpx.Timeout(
            timeout=float(settings.ANTIBOT_TIMEOUT),
            connect=self._settings.CONNECT_TIMEOUT,
        )
        self._tracker: dict[str, int] = {
            "used": 0,
            "limit": self._DEFAULT_CREDIT_LIMIT,
            "month": 0,
        }

    def _api_key(self) -> str:
        """Return the API key for this service. Subclasses must override."""
        raise NotImplementedError

    def _build_url(self, target_url: str) -> str:
        """Build the full API URL from the template, key, and encoded target."""
        encoded = quote(target_url, safe="")
        return self._API_URL_TEMPLATE.format(key=self._api_key(), url=encoded)

    async def fetch(self, url: str) -> FetchResult:
        """Fetch a URL through the anti-bot service.

        Credit tracking: monthly limit. Counter resets on calendar month boundary.
        If credits are exhausted, returns immediately without making an HTTP call.
        """
        api_key = self._api_key()
        if not api_key:
            log.info("%s fetch skipped: API key not set", self._SERVICE_NAME)
            return FetchResult(
                success=False,
                url=url,
                error=f"{self._SERVICE_NAME} API key not configured",
                source=self._SOURCE,
            )

        current_month = _current_month_key()
        # Read persisted usage from SQLite store
        used = self._quota_store.get_used(self._SERVICE_NAME, current_month)
        limit: int = self._tracker["limit"]
        self._tracker["month"] = current_month
        self._tracker["used"] = used

        if not _check_credits(used, limit):
            log.warning(
                "%s credit limit reached (%d/%d) — refusing request",
                self._SERVICE_NAME,
                used,
                limit,
            )
            return FetchResult(
                success=False,
                url=url,
                error="credit limit reached",
                source=self._SOURCE,
            )

        log.info("%s fetch: %s", self._SERVICE_NAME, url)
        scrape_url = self._build_url(url)

        result = await safe_fetch(
            self._client,
            method="GET",
            url=scrape_url,
            source=self._SOURCE,
            timeout=self._timeout,
        )

        # Increment persistent credit counter on success
        if result.success:
            new_used = self._quota_store.increment(self._SERVICE_NAME, current_month)
            self._tracker["used"] = new_used
            log.info(
                "%s fetch succeeded for %s — credits used: %d/%d",
                self._SERVICE_NAME,
                url,
                new_used,
                limit,
            )

        return result
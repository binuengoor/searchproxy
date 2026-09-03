"""Tier 0 fast HTTP fetcher using direct HTTP GET and Trafilatura."""

from __future__ import annotations

import logging
import re
import time
import httpx

from app.config import Settings
from app.services.content_cleaner import clean_content
from app.services.models import FetchResult

log = logging.getLogger(__name__)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Common anti-bot indicators
_ANTI_BOT_PATTERNS = [
    re.compile(r"cloudflare", re.IGNORECASE),
    re.compile(r"just a moment", re.IGNORECASE),
    re.compile(r"checking your browser", re.IGNORECASE),
    re.compile(r"ddos-guard", re.IGNORECASE),
    re.compile(r"security check", re.IGNORECASE),
]


def _is_blocked(status_code: int, text: str) -> bool:
    if status_code in (403, 503):
        return True
    lower_head = text[:2000].lower()
    return any(p.search(lower_head) for p in _ANTI_BOT_PATTERNS)


class FastFetchClient:
    """Ultra-fast raw HTTP GET fetcher with Trafilatura extraction (Tier 0)."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def fetch(self, url: str) -> FetchResult:
        """Fetch raw HTML and extract text.

        Fails quickly if the page requires JS, triggers anti-bot, or times out,
        allowing FetchChain to fall through to Crawl4AI/Byparr.
        """
        start = time.perf_counter()
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        timeout = httpx.Timeout(
            timeout=float(self._settings.FAST_FETCH_TIMEOUT),
            connect=float(self._settings.CONNECT_TIMEOUT),
        )

        try:
            response = await self._client.get(
                url,
                headers=headers,
                timeout=timeout,
                follow_redirects=True,
            )
            elapsed = (time.perf_counter() - start) * 1000.0

            if _is_blocked(response.status_code, response.text):
                log.info("FastFetch: Anti-bot block detected for '%s' (%d)", url, response.status_code)
                return FetchResult(
                    success=False,
                    url=url,
                    status_code=response.status_code,
                    error="Anti-bot challenge detected",
                    source="http_fast",
                    fetch_time_ms=elapsed,
                )

            if response.status_code != 200:
                log.info("FastFetch: HTTP %d for '%s'", response.status_code, url)
                return FetchResult(
                    success=False,
                    url=url,
                    status_code=response.status_code,
                    error=f"HTTP {response.status_code}",
                    source="http_fast",
                    fetch_time_ms=elapsed,
                )

            html = response.text
            # Extract title if present
            title_match = _TITLE_RE.search(html)
            title = title_match.group(1).strip() if title_match else ""

            # Extract markdown via Trafilatura
            markdown = clean_content(html)
            if not markdown or len(markdown) < self._settings.RETRIEVE_MIN_CONTENT_LENGTH:
                log.info("FastFetch: Content too short (%d chars) for '%s' (likely SPA)", len(markdown), url)
                return FetchResult(
                    success=False,
                    url=url,
                    status_code=200,
                    error="Content too short or requires JavaScript rendering",
                    source="http_fast",
                    fetch_time_ms=elapsed,
                )

            log.info("FastFetch: Succeeded for '%s' (%d chars) in %.1fms", url, len(markdown), elapsed)
            return FetchResult(
                success=True,
                url=url,
                markdown=markdown,
                title=title,
                status_code=200,
                source="http_fast",
                fetch_time_ms=elapsed,
            )

        except httpx.TimeoutException:
            elapsed = (time.perf_counter() - start) * 1000.0
            log.info("FastFetch: Timed out for '%s' in %.1fms", url, elapsed)
            return FetchResult(
                success=False,
                url=url,
                error="FastFetch timeout",
                source="http_fast",
                fetch_time_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000.0
            log.info("FastFetch: Failed for '%s': %s", url, exc)
            return FetchResult(
                success=False,
                url=url,
                error=str(exc),
                source="http_fast",
                fetch_time_ms=elapsed,
            )

"""Reddit thread extractor — fetches posts and comments via Reddit JSON API
and formats into clean Markdown.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.services.models import FetchResult

log = logging.getLogger(__name__)

# URL regex for Reddit thread matching
_REDDIT_THREAD_PATTERN = re.compile(
    r"https?://(?:(?:www|old|new|np)\.)?reddit\.com/(?:r/[^/]+/)?comments/([a-z0-9]+)",
    re.IGNORECASE,
)
_REDDIT_SHORT_PATTERN = re.compile(
    r"https?://redd\.it/([a-z0-9]+)",
    re.IGNORECASE,
)
_REDDIT_GALLERY_PATTERN = re.compile(
    r"https?://(?:(?:www|old|new|np)\.)?reddit\.com/gallery/([a-z0-9]+)",
    re.IGNORECASE,
)
_REDDIT_SHARE_PATTERN = re.compile(
    r"https?://(?:(?:www|old|new|np)\.)?reddit\.com/(?:r/[^/]+/)?s/([a-zA-Z0-9]+)",
    re.IGNORECASE,
)

# Standard browser fingerprint headers (Chrome 133 / macOS)
_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/133.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="133", "Not(A:Brand";v="99", "Google Chrome";v="133"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class RedditClient:
    """Specialized client for Reddit post and discussion extraction."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._timeout = float(settings.REDDIT_TIMEOUT)
        self._comment_limit = settings.REDDIT_COMMENT_LIMIT
        self._max_depth = settings.REDDIT_COMMENT_DEPTH

    @staticmethod
    def is_reddit_url(url: str) -> bool:
        """Check if a given URL is a Reddit post or thread."""
        return bool(
            _REDDIT_THREAD_PATTERN.search(url)
            or _REDDIT_SHORT_PATTERN.search(url)
            or _REDDIT_GALLERY_PATTERN.search(url)
            or _REDDIT_SHARE_PATTERN.search(url)
        )

    @staticmethod
    def extract_post_id(url: str) -> str | None:
        """Extract the base36 post ID from a Reddit URL."""
        match = _REDDIT_THREAD_PATTERN.search(url)
        if match:
            return match.group(1)
        match = _REDDIT_SHORT_PATTERN.search(url)
        if match:
            return match.group(1)
        match = _REDDIT_GALLERY_PATTERN.search(url)
        if match:
            return match.group(1)
        return None

    def _resolve_cookies(self) -> dict[str, str]:
        """Resolve Reddit authentication cookies from settings or local credentials file."""
        # 1. Explicit environment variable / setting
        if self._settings.REDDIT_SESSION_COOKIE:
            return {"reddit_session": self._settings.REDDIT_SESSION_COOKIE.strip()}

        # 2. Configured credential file path
        candidate_paths: list[Path] = []
        if self._settings.REDDIT_CREDENTIAL_PATH:
            candidate_paths.append(Path(self._settings.REDDIT_CREDENTIAL_PATH))

        # 3. Default rdt-cli credential location (~/.config/rdt-cli/credential.json)
        default_cred = Path.home() / ".config" / "rdt-cli" / "credential.json"
        candidate_paths.append(default_cred)

        for path in candidate_paths:
            if path.is_file():
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    cookies = data.get("cookies", {})
                    if isinstance(cookies, dict) and cookies:
                        return {str(k): str(v) for k, v in cookies.items()}
                except Exception as exc:
                    log.debug("Failed reading credentials from %s: %s", path, exc)

        return {}

    async def fetch(self, url: str) -> FetchResult:
        """Fetch post and comments from Reddit JSON API and format into Markdown."""
        target_url = url
        if _REDDIT_SHARE_PATTERN.search(url):
            try:
                resp = await self._client.get(
                    url,
                    headers=_BROWSER_HEADERS,
                    follow_redirects=False,
                    timeout=self._timeout,
                )
                loc = resp.headers.get("location")
                if loc:
                    target_url = loc
            except Exception as exc:
                log.warning("Failed resolving Reddit share URL %s: %s", url, exc)

        post_id = self.extract_post_id(target_url)
        if not post_id:
            return FetchResult(
                success=False,
                url=url,
                error="Could not extract Reddit post ID from URL",
                source="reddit_json",
            )

        api_url = (
            f"https://www.reddit.com/comments/{post_id}.json"
            f"?raw_json=1&limit={self._comment_limit}&sort=best"
        )
        cookies = self._resolve_cookies()

        try:
            resp = await self._client.get(
                api_url,
                headers=_BROWSER_HEADERS,
                cookies=cookies,
                timeout=self._timeout,
                follow_redirects=True,
            )

            if resp.status_code != 200:
                log.warning("Reddit API returned HTTP %s for %s", resp.status_code, url)
                return FetchResult(
                    success=False,
                    url=url,
                    status_code=resp.status_code,
                    error=f"Reddit API returned HTTP {resp.status_code}",
                    source="reddit_json",
                )

            data = resp.json()
            if not isinstance(data, list) or len(data) < 2:
                return FetchResult(
                    success=False,
                    url=url,
                    status_code=resp.status_code,
                    error="Unexpected response structure from Reddit JSON API",
                    source="reddit_json",
                )

            markdown, title, subreddit, author = self._format_thread(data, original_url=url)

            return FetchResult(
                success=True,
                url=url,
                markdown=markdown,
                title=title,
                description=f"Reddit discussion in r/{subreddit} by u/{author}",
                status_code=resp.status_code,
                source="reddit_json",
            )

        except httpx.TimeoutException:
            log.warning("Reddit fetch timed out for %s", url)
            return FetchResult(
                success=False,
                url=url,
                error="Reddit fetch timed out",
                source="reddit_json",
            )
        except Exception as exc:
            log.warning("Reddit fetch failed for %s: %s", url, exc)
            return FetchResult(
                success=False,
                url=url,
                error=str(exc),
                source="reddit_json",
            )

    def _format_thread(
        self,
        data: list[dict[str, Any]],
        original_url: str,
    ) -> tuple[str, str, str, str]:
        """Convert Reddit JSON payload into structured Markdown."""
        post_listing = data[0].get("data", {}).get("children", [])
        post_data = post_listing[0].get("data", {}) if post_listing else {}

        title = post_data.get("title", "Reddit Post")
        subreddit = post_data.get("subreddit", "reddit")
        author = post_data.get("author", "[unknown]")
        score = post_data.get("score", 0)
        upvote_ratio = post_data.get("upvote_ratio")
        selftext = post_data.get("selftext", "").strip()
        permalink = post_data.get("permalink", "")
        full_permalink = f"https://www.reddit.com{permalink}" if permalink else original_url
        external_url = post_data.get("url", "")
        created_utc = post_data.get("created_utc")

        date_str = ""
        if created_utc:
            try:
                date_str = datetime.datetime.fromtimestamp(
                    created_utc, tz=datetime.timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                pass

        # Build Post Header
        lines: list[str] = [f"# {title}", ""]
        meta_items: list[str] = [
            f"**Subreddit:** r/{subreddit}",
            f"**Author:** u/{author}",
            f"**Score:** +{score}",
        ]
        if upvote_ratio is not None:
            meta_items.append(f"({int(upvote_ratio * 100)}% upvoted)")
        if date_str:
            meta_items.append(f"**Posted:** {date_str}")
        meta_items.append(f"[Thread Link]({full_permalink})")

        lines.append(" | ".join(meta_items))
        lines.append("")

        # Post body or external link
        if external_url and external_url != full_permalink and not external_url.startswith(f"https://www.reddit.com{permalink}"):
            lines.append(f"**Linked URL:** [{external_url}]({external_url})")
            lines.append("")

        if selftext:
            lines.append(selftext)
            lines.append("")

        # Comments
        comment_listing = data[1].get("data", {}).get("children", [])
        comment_lines = self._format_comments(comment_listing, current_depth=1)

        if comment_lines:
            lines.append("---")
            lines.append("## Comments Section")
            lines.append("")
            lines.extend(comment_lines)

        return "\n".join(lines).strip(), title, subreddit, author

    def _format_comments(
        self,
        comments: list[dict[str, Any]],
        current_depth: int,
    ) -> list[str]:
        """Format a list of comment nodes into markdown with indentations/quotes."""
        lines: list[str] = []

        for item in comments:
            kind = item.get("kind")
            data = item.get("data", {})

            if kind != "t1":
                continue

            author = data.get("author", "[deleted]")
            score = data.get("score", 0)
            body = (data.get("body") or "").strip()

            if not body or body in ("[deleted]", "[removed]"):
                continue

            if current_depth == 1:
                lines.append(f"### u/{author} (+{score})")
                lines.append(body)
                lines.append("")
            else:
                # Use markdown blockquote for nested replies
                prefix = "> " * (current_depth - 1)
                lines.append(f"{prefix}**u/{author}** (+{score}):")
                indented_body = "\n".join(
                    f"{prefix}{line}" if line else prefix.rstrip()
                    for line in body.splitlines()
                )
                lines.append(indented_body)
                lines.append("")

            # Handle replies
            if current_depth < self._max_depth:
                replies = data.get("replies")
                if isinstance(replies, dict):
                    child_comments = replies.get("data", {}).get("children", [])
                    if child_comments:
                        child_lines = self._format_comments(
                            child_comments, current_depth=current_depth + 1
                        )
                        lines.extend(child_lines)

        return lines

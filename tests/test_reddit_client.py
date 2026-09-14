"""Unit tests for RedditClient and FetchChain Reddit extraction."""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import Settings
from app.services.fetch_chain import FetchChain
from app.services.models import FetchResult
from app.services.reddit_client import RedditClient

# ── Sample Reddit JSON Fixture ────────────────────────────────────────

SAMPLE_REDDIT_JSON = [
    {
        "kind": "Listing",
        "data": {
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "id": "1wfhw67",
                        "title": "my minimal Pi setup, personas + tmux subagents",
                        "author": "Sweet-Transition-787",
                        "subreddit": "PiCodingAgent",
                        "score": 108,
                        "upvote_ratio": 0.96,
                        "selftext": (
                            "My Pi setup, kept minimal on purpose.\n\n"
                            "I run kitty + tmux + fish."
                        ),
                        "permalink": "/r/PiCodingAgent/comments/1wfhw67/my_setup/",
                        "url": "https://www.reddit.com/r/PiCodingAgent/comments/1wfhw67/my_setup/",
                        "is_self": True,
                        "created_utc": 1742034600,
                    },
                }
            ]
        },
    },
    {
        "kind": "Listing",
        "data": {
            "children": [
                {
                    "kind": "t1",
                    "data": {
                        "id": "c1",
                        "author": "Aggressive-Dream5465",
                        "score": 14,
                        "body": "What 'natural-english-writer' is exactly? Is it an AI humaniser?",
                        "replies": {
                            "kind": "Listing",
                            "data": {
                                "children": [
                                    {
                                        "kind": "t1",
                                        "data": {
                                            "id": "c2",
                                            "author": "Sweet-Transition-787",
                                            "score": 9,
                                            "body": "Yes Astra wrote it it works pretty well.",
                                            "replies": "",
                                        },
                                    }
                                ]
                            },
                        },
                    },
                },
                {
                    "kind": "t1",
                    "data": {
                        "id": "c3",
                        "author": "[deleted]",
                        "score": 1,
                        "body": "[deleted]",
                        "replies": "",
                    },
                },
            ]
        },
    },
]


# ── URL Detection & Extraction Tests ─────────────────────────────────

def test_is_reddit_url():
    valid_urls = [
        "https://www.reddit.com/r/PiCodingAgent/comments/1wfhw67/my_minimal_pi_setup/",
        "http://reddit.com/r/homelab/comments/123456",
        "https://old.reddit.com/r/selfhosted/comments/abcdef/my_setup/",
        "https://redd.it/1wfhw67",
        "https://new.reddit.com/comments/xyz123/",
        "https://www.reddit.com/gallery/1wfhw67",
    ]
    for u in valid_urls:
        assert RedditClient.is_reddit_url(u) is True, f"Failed for {u}"

    invalid_urls = [
        "https://google.com",
        "https://github.com/badlogic/pi-mono",
        "https://en.wikipedia.org/wiki/Reddit",
        "https://reddit.com/user/someone",
        "https://reddit.com/r/homelab/",  # subreddit listing, not a post
    ]
    for u in invalid_urls:
        assert RedditClient.is_reddit_url(u) is False, f"Incorrectly matched {u}"


def test_extract_post_id():
    url = "https://www.reddit.com/r/PiCodingAgent/comments/1wfhw67/title/"
    assert RedditClient.extract_post_id(url) == "1wfhw67"
    assert RedditClient.extract_post_id("https://redd.it/abcdef") == "abcdef"
    assert RedditClient.extract_post_id("https://old.reddit.com/comments/999xyz") == "999xyz"
    assert RedditClient.extract_post_id("https://example.com/not-reddit") is None


# ── Cookie Resolution Tests ──────────────────────────────────────────

def test_cookie_resolution_from_setting(tmp_path):
    settings = Settings(REDDIT_SESSION_COOKIE="test_session_token_123")
    client = RedditClient(client=MagicMock(), settings=settings)
    cookies = client._resolve_cookies()
    assert cookies == {"reddit_session": "test_session_token_123"}


def test_cookie_resolution_from_file(tmp_path):
    cred_file = tmp_path / "credential.json"
    cred_payload = {"cookies": {"reddit_session": "file_token_456", "loid": "abc"}}
    cred_file.write_text(json.dumps(cred_payload))

    settings = Settings(REDDIT_SESSION_COOKIE=None, REDDIT_CREDENTIAL_PATH=str(cred_file))
    client = RedditClient(client=MagicMock(), settings=settings)
    cookies = client._resolve_cookies()
    assert cookies == {"reddit_session": "file_token_456", "loid": "abc"}


# ── Markdown Formatting Tests ────────────────────────────────────────

def test_format_thread():
    settings = Settings()
    client = RedditClient(client=MagicMock(), settings=settings)

    markdown, title, subreddit, author = client._format_thread(
        SAMPLE_REDDIT_JSON,
        original_url="https://www.reddit.com/r/PiCodingAgent/comments/1wfhw67/title/",
    )

    assert title == "my minimal Pi setup, personas + tmux subagents"
    assert subreddit == "PiCodingAgent"
    assert author == "Sweet-Transition-787"

    # Verify post elements
    assert "# my minimal Pi setup, personas + tmux subagents" in markdown
    assert "**Subreddit:** r/PiCodingAgent" in markdown
    assert "**Author:** u/Sweet-Transition-787" in markdown
    assert "**Score:** +108" in markdown
    assert "My Pi setup, kept minimal on purpose." in markdown

    # Verify comments section
    assert "## Comments Section" in markdown
    assert "### u/Aggressive-Dream5465 (+14)" in markdown
    assert "What 'natural-english-writer' is exactly?" in markdown

    # Verify nested reply formatting
    assert "> **u/Sweet-Transition-787** (+9):" in markdown
    assert "> Yes Astra wrote it it works pretty well." in markdown

    # Verify deleted comment is omitted
    assert "[deleted]" not in markdown


# ── Async Fetch Tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reddit_fetch_success():
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = SAMPLE_REDDIT_JSON
    mock_http.get.return_value = mock_resp

    settings = Settings()
    client = RedditClient(client=mock_http, settings=settings)

    res = await client.fetch("https://www.reddit.com/r/PiCodingAgent/comments/1wfhw67/test/")
    assert res.success is True
    assert res.status_code == 200
    assert res.source == "reddit_json"
    assert "Sweet-Transition-787" in res.markdown
    assert "Aggressive-Dream5465" in res.markdown


@pytest.mark.asyncio
async def test_reddit_fetch_http_error():
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 403
    mock_http.get.return_value = mock_resp

    settings = Settings()
    client = RedditClient(client=mock_http, settings=settings)

    res = await client.fetch("https://www.reddit.com/r/PiCodingAgent/comments/1wfhw67/test/")
    assert res.success is False
    assert res.status_code == 403
    assert "HTTP 403" in res.error


# ── FetchChain Integration Tests ─────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_chain_reddit_integration():
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    settings = Settings(FAST_FETCH_ENABLED=True)

    chain = FetchChain(client=mock_http, settings=settings)

    # Mock the internal reddit client
    chain._reddit.fetch = AsyncMock(
        return_value=FetchResult(
            success=True,
            url="https://reddit.com/comments/1wfhw67",
            markdown="# Mocked Reddit Thread Content",
            title="Mocked Reddit Thread",
            status_code=200,
            source="reddit_json",
        )
    )

    result = await chain.execute("https://reddit.com/comments/1wfhw67")
    assert result.success is True
    assert result.source == "reddit_json"
    assert result.title == "Mocked Reddit Thread"
    assert result.markdown == "# Mocked Reddit Thread Content"
    chain._reddit.fetch.assert_awaited_once_with("https://reddit.com/comments/1wfhw67")

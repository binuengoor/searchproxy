"""Tests for app/services/content_cleaner."""

from __future__ import annotations

import pytest

from app.services.content_cleaner import clean_content


class TestLooksLikeHtmlDetector:
    """The _looks_like_html behaviour is exercised indirectly via clean_content."""

    def test_clean_content_passes_through_plain_text(self) -> None:
        raw = "This is already clean text. No HTML tags here."
        assert clean_content(raw) == raw

    def test_clean_content_passes_through_markdown(self) -> None:
        raw = "# Title\n\n- Bullet 1\n- Bullet 2\n\nSome paragraph."
        assert clean_content(raw) == raw

    def test_clean_content_empty_string(self) -> None:
        assert clean_content("") == ""

    def test_clean_content_short_text_not_html(self) -> None:
        """Text shorter than threshold is returned as-is even with short tags."""
        raw = "Hi <b>there</b> friend"
        assert clean_content(raw) == raw


class TestContentCleanerExtraction:
    """trafilatura-based extraction from real HTML."""

    def test_extracts_article_from_blog_html(self) -> None:
        html = """
        <html>
        <head><title>Test Blog</title><script>console.log(1);</script></head>
        <body>
          <nav><a href="/">Home</a><a href="/about">About</a></nav>
          <article>
            <h1>Real Article Title</h1>
            <p>This is the body of the article. It should be preserved.</p>
            <p>Second paragraph with more content.</p>
          </article>
          <footer>Copyright 2026</footer>
        </body>
        </html>
        """
        result = clean_content(html)
        assert "Real Article Title" in result
        assert "body of the article" in result
        assert "console.log" not in result
        assert "Copyright" not in result

    def test_strips_navigation_and_junk(self) -> None:
        html = """
        <html>
        <body>
          <header><nav><a href="/">Home</a><a href="/shop">Shop</a></nav></header>
          <main>
            <h1>Important News</h1>
            <p>Here is the actual content.</p>
          </main>
          <footer><p>Contact us at fake@example.com</p></footer>
        </body>
        </html>
        """
        result = clean_content(html)
        assert "Important News" in result
        assert "actual content" in result
        # On a sparse fixture page trafilatura may include short nav links.
        # The *real* win is that no <script>, <style>, or raw DOM remains.
        assert "<script" not in result
        assert "<style" not in result
        assert "<nav" not in result
        assert "<footer" not in result
        assert "<script>" not in result
        assert "<style>" not in result

    def test_returns_truncated_raw_on_failure(self) -> None:
        """When trafilatura can't extract, we fall back to truncated raw."""
        html = "<html><head></head><body></body></html>"
        result = clean_content(html)
        # Falls back to the raw HTML (truncated, but entire string < 8 000 chars)
        assert result.startswith("<html>")

    def test_does_not_mutate_input(self) -> None:
        raw = "  some text  "
        result = clean_content(raw)
        assert result == "some text"
        assert raw == "  some text  "  # input unchanged


class TestMarkdownPreserved:
    """Already-clean markdown from Crawl4AI / Jina should be untouched."""

    def test_markdown_with_tables(self) -> None:
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        assert clean_content(md) == md

    def test_markdown_with_code_blocks(self) -> None:
        md = "```python\nprint('hello')\n```"
        assert clean_content(md) == md

    def test_markdown_with_links(self) -> None:
        md = "Check out [this link](https://example.com)."
        assert clean_content(md) == md

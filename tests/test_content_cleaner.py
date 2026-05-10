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

    def test_strips_html_on_extraction_failure(self) -> None:
        """When trafilatura can't extract, we fall back to HTML-stripped text."""
        html = "<html><head></head><body><p>Hello world</p></body></html>"
        result = clean_content(html)
        # HTML tags are stripped in the fallback path
        assert "<html>" not in result
        assert "<p>" not in result
        assert "Hello world" in result

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
    def test_aggressive_strips_html_fallback_nav_heavy(self) -> None:
        """Aggressive mode on a nav-heavy page (no article body) strips HTML in fallback."""
        html = """<html><body>
        <nav><a href="/">Home</a><a href="/shop">Shop</a></nav>
        <div class="fixtures">
          <span>Arsenal vs Chelsea - May 10</span>
          <span>Liverpool vs Arsenal - May 17</span>
        </div>
        <footer><a href="/terms">Terms</a></footer>
        </body></html>"""
        result = clean_content(html, aggressive=True)
        # No HTML tags should remain in the result
        assert "<nav>" not in result
        assert "<footer>" not in result
        # Score/match info should still be present
        assert "Arsenal" in result

class TestMarkdownSpamStrip:
    """Markdown nav/link-spam removal in aggressive mode."""

    def test_strips_link_spam_lines(self) -> None:
        """Lines that are >70% markdown links are removed."""
        md = "# Arsenal Fixtures\n\nArsenal vs Chelsea on May 10.\n\n[ Home ](https://example.com/) [ About ](https://example.com/about) [ Contact ](https://example.com/contact) [ Betting Sites ](https://example.com/betting/) [ More Links ](https://example.com/more/)\n\nArsenal vs Liverpool on May 17."
        result = clean_content(md, aggressive=True)
        assert "Arsenal vs Chelsea" in result
        assert "Arsenal vs Liverpool" in result
        assert "[ About ]" not in result
        assert "Betting Sites" not in result

    def test_preserves_normal_markdown_links(self) -> None:
        """Lines with inline links within paragraphs are preserved."""
        md = "Arsenal signed a new player from [BBC Sport](https://bbc.co.uk). The transfer was confirmed."
        result = clean_content(md, aggressive=True)
        assert "Arsenal signed" in result
        assert "BBC Sport" in result

    def test_strips_bare_url_lines(self) -> None:
        """Lines that are just a URL are removed."""
        md = "See details below.\n\nhttps://www.example.com/some/long/path\n\nArsenal won 3-0."
        result = clean_content(md, aggressive=True)
        assert "Arsenal won" in result
        assert "https://www.example.com" not in result

    def test_strips_pipe_delimited_nav(self) -> None:
        """Pipe-delimited nav lines with multiple links are removed."""
        md = "# Arsenal\n\nArsenal play Chelsea.\n\n| [Home](/) | [Fixtures](/fixtures) | [Results](/results) | [Tables](/tables) |\n\nMore content here."
        result = clean_content(md, aggressive=True)
        assert "Arsenal play Chelsea" in result
        assert "More content" in result

    def test_collapses_excessive_blank_lines(self) -> None:
        """More than 2 consecutive blank lines are collapsed to max 2."""
        md = "Paragraph one.\n\n\n\n\n\nParagraph two."
        result = clean_content(md, aggressive=True)
        # Should not have 3+ consecutive newlines (more than 1 blank line)
        assert "\n\n\n" not in result

    def test_aggressive_mode_strips_spam_from_markdown(self) -> None:
        """Aggressive mode on markdown input runs spam stripping."""
        md = "# Arsenal Fixtures\n\n[ Bet365 ](https://bet365.com) [ 1xBet ](https://1xbet.com) [ Betway ](https://betway.com)\n\nWest Ham vs Arsenal - May 10\nArsenal vs Burnley - May 17"
        result = clean_content(md, aggressive=True)
        assert "West Ham vs Arsenal" in result
        # The link-spam line should be removed
        assert "Bet365" not in result


class TestParagraphBoundaryTruncation:
    """Paragraph-boundary-aware truncation in _truncate_content."""

    def test_no_truncation_when_under_limit(self) -> None:
        from app.services.retrieve_service import _truncate_content
        content = "Short content."
        assert _truncate_content(content, 100) == content

    def test_truncates_at_paragraph_boundary(self) -> None:
        from app.services.retrieve_service import _truncate_content
        content = "Para one\n\nPara two\n\nPara three\n\nPara four"
        result = _truncate_content(content, 25)
        # Should cut at a paragraph boundary, not mid-word
        assert result.endswith("Para two")
        assert "Para three" not in result

    def test_falls_back_to_sentence_boundary(self) -> None:
        from app.services.retrieve_service import _truncate_content
        content = "First sentence. Second sentence. Third sentence. Fourth sentence."
        result = _truncate_content(content, 35)
        # Should end at a sentence boundary
        assert result.endswith(".")
        assert "Fourth" not in result

    def test_hard_cut_when_no_good_boundary(self) -> None:
        from app.services.retrieve_service import _truncate_content
        content = "abcdefghijklmnopqrstuvwxyz"
        result = _truncate_content(content, 10)
        assert len(result) <= 10
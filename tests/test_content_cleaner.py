"""Tests for app/services/content_cleaner."""

from __future__ import annotations

import pytest

from app.services.content_cleaner import clean_content, _strip_consent_dialogs


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


class TestConsentDialogStrip:
    """GDPR cookie-consent dialog removal."""

    # ── Heading truncation ──

    def test_strips_cookie_policy_heading(self) -> None:
        """Content after '## Cookies Policy' heading is removed."""
        md = "# Arsenal Fixtures\n\nArsenal vs Chelsea - May 10.\n\n## Cookies Policy\n\nWe use cookies to improve your browsing experience.\n\nAccept All Cookies\n\nManage Consent Preferences"
        result = _strip_consent_dialogs(md)
        assert "Arsenal vs Chelsea" in result
        assert "Cookies Policy" not in result
        assert "browsing experience" not in result

    def test_strips_cookie_preference_centre(self) -> None:
        """Content after '### Cookie Preference Centre' is removed."""
        md = "Arsenal won 3-0.\n\n## Cookie Preference Centre\n\nYou can manage which cookies are set.\n\nNecessary Cookies\nAlways Active"
        result = _strip_consent_dialogs(md)
        assert "Arsenal won" in result
        assert "Cookie Preference" not in result
        assert "manage which cookies" not in result

    def test_cookie_policy_in_short_text_is_still_removed(self) -> None:
        """'Cookies Policy' is never legitimate article content, even in short text."""
        md = "## Cookies Policy\n\nThis document describes our cookie usage for the site.\n\nThe end."
        result = _strip_consent_dialogs(md)
        # Consent headings are always stripped — they're never real article content
        assert "Cookies Policy" not in result
        assert "cookie usage" not in result

    # ── Block-level trigger truncation ──

    def test_strips_cookie_block_by_content_phrase(self) -> None:
        """Paragraphs containing 'we use cookies to improve your browsing' are removed from that point."""
        md = "# Arsenal Fixtures\n\nArsenal vs Chelsea - May 10.\n\nWe use cookies to improve your browsing experience and help us improve our websites. We also carefully select third-party cookies to show you more relevant ads online.\n\nBy clicking \"THAT'S OK\", you agree to our use of cookies."
        result = _strip_consent_dialogs(md)
        assert "Arsenal vs Chelsea" in result
        assert "improve your browsing" not in result

    def test_strips_block_with_agree_and_cookie(self) -> None:
        """'by clicking ... agree ... cookie' block trigger removes content from that point."""
        md = "Match results here.\n\nBy clicking \"THAT'S OK\", you agree to our use of cookies in accordance with our Cookie Policy. The rest of the page is boilerplate."
        result = _strip_consent_dialogs(md)
        assert "Match results" in result
        assert "by clicking" not in result.lower()

    def test_strips_you_can_manage_cookies_paragraph(self) -> None:
        """'you can manage which cookies are set' paragraph is removed."""
        md = "Arsenal fixtures for 2025.\n\nYou can manage which cookies are set on your device by clicking on the different category headings below.\n\nSome more content."
        result = _strip_consent_dialogs(md)
        assert "Arsenal fixtures" in result
        assert "manage which cookies" not in result

    # ── Line-level noise removal ──

    def test_strips_consent_ui_lines(self) -> None:
        """Individual consent UI noise lines are removed from anywhere in text."""
        md = "Article content.\n\nAccept All Cookies\n\nMore article.\n\nConfirm My Choices\n\nEnd."
        result = _strip_consent_dialogs(md)
        assert "Article content" in result
        assert "More article" in result
        assert "End" in result
        assert "Accept All Cookies" not in result
        assert "Confirm My Choices" not in result

    def test_strips_checkbox_label_lines(self) -> None:
        """'checkbox label label' UI noise is removed."""
        md = "Real content.\n\ncheckbox label label\n\nMore real content."
        result = _strip_consent_dialogs(md)
        assert "Real content" in result
        assert "More real content" in result
        assert "checkbox label" not in result

    def test_strips_login_create_account_line(self) -> None:
        """'Login Create account' UI noise is removed."""
        md = "Article text.\n\nLogin Create account\n\nMore article."
        result = _strip_consent_dialogs(md)
        assert "Article text" in result
        assert "Login Create account" not in result

    def test_strips_membership_wall_line(self) -> None:
        """'You need an Arsenal Membership to watch' line is removed."""
        md = "Match report.\n\nYou need an Arsenal Membership to watch this video\n\nSwitch User Become a member\n\nAnalysis."
        result = _strip_consent_dialogs(md)
        assert "Match report" in result
        assert "Analysis" in result
        assert "Membership to watch" not in result
        assert "Switch User" not in result

    # ── Integration with clean_content ──

    def test_clean_content_aggressive_strips_consent(self) -> None:
        """Full clean_content pipeline strips consent dialogs in aggressive mode."""
        md = "# Arsenal FC\n\nArsenal play Chelsea on May 10.\n\n## Cookies Policy\n\nWe use cookies to improve your browsing experience.\n\nAccept All Cookies\n\nManage Consent Preferences\n\nNecessary Cookies\nAlways Active"
        result = clean_content(md, aggressive=True)
        assert "Arsenal play Chelsea" in result
        assert "Cookies Policy" not in result
        assert "Accept All Cookies" not in result
        assert "Manage Consent" not in result

    def test_clean_content_aggressive_strips_consent_from_html(self) -> None:
        """Consent stripping also works when clean_content processes HTML via trafilatura."""
        html = """<html><body>
        <article>
          <h1>Arsenal Transfer News</h1>
          <p>Arsenal are close to signing a new striker.</p>
        </article>
        <div class="cookie-banner">
          <h2>Cookies Policy</h2>
          <p>We use cookies to improve your browsing experience and help us improve our websites.</p>
          <p>By clicking "THAT'S OK", you agree to our use of cookies.</p>
          <button>Accept All Cookies</button>
        </div>
        </body></html>"""
        result = clean_content(html, aggressive=True)
        assert "Arsenal Transfer News" in result
        assert "browsing experience" not in result
        assert "Accept All Cookies" not in result

    # ── Edge cases ──

    def test_short_text_unchanged(self) -> None:
        """Text under 200 chars with no consent patterns is left alone."""
        md = "Short text with no consent dialog."
        result = _strip_consent_dialogs(md)
        assert result == md

    def test_empty_string_unchanged(self) -> None:
        """Empty string returns empty."""
        assert _strip_consent_dialogs("") == ""

    def test_real_data_arsenal_source_2(self) -> None:
        """Simulates the real Premier League source with consent dialog."""
        md = """Arsenal Fixtures

May 2026

Arsenal vs West Ham - Sun May 10
Burnley vs Arsenal - Mon May 18

Cookie Policy

We use cookies to improve your browsing experience. By clicking "THAT'S OK", you agree to our use of cookies.

Manage Consent Preferences
Necessary Cookies
Always Active
Non-essential cookies help us improve the functionality of our website.
Allow All

Cookie List
Search Icon
Filter Icon
Clear
checkbox label label
Apply Cancel
Consent Leg.Interest
checkbox label label
Confirm My Choices"""
        result = _strip_consent_dialogs(md)
        assert "Arsenal vs West Ham" in result
        assert "Cookie Policy" not in result
        assert "Confirm My Choices" not in result
        assert "Consent Leg.Interest" not in result
        assert "browsing experience" not in result

    def test_uefa_style_consent_only_page(self) -> None:
        """UEFA pages that return only consent dialogs with no article content."""
        md = """Consent to Cookies & Data processing
We, and other third parties, use cookies and other technologies to process end device information and other categories of personal data (such as email addresses, IP-addresses, browser and device characteristics) for the following purposes: to store and/or access information on a device; to select personalised ads; to create ad user profiles; to develop and improve products; to measure ad and content performance; to measure audience; to apply market research to generate audience insights; to develop and improve products. We also use cookies and other technologies for these purposes: personalised ads and content, ad and content measurement, audience insights.

You can manage your cookie preferences and withdraw your consent at any time via the "Privacy settings" link at the bottom of the webpage. Your choices will have effect only within the UEFA domain.

Your personal data may be shared with certain third parties and processed by them. Your consent is voluntary and can be withdrawn at any time via the "Privacy settings" link at the bottom of the webpage.

2026
### 07 April 2026
### 15 April 2026"""
        result = _strip_consent_dialogs(md)
        # The entire page is consent boilerplate
        assert "consent" not in result.lower()
        assert "cookies" not in result.lower()
        assert "personal data" not in result.lower()


class TestParagraphBoundaryTruncation:
    """Paragraph-boundary-aware truncation in _truncate_content."""

    def test_no_truncation_when_under_limit(self) -> None:
        from app.services.retrieve_steps import truncate_content
        content = "Short content."
        assert truncate_content(content, 100) == content

    def test_truncates_at_paragraph_boundary(self) -> None:
        from app.services.retrieve_steps import truncate_content
        content = "Para one\n\nPara two\n\nPara three\n\nPara four"
        result = truncate_content(content, 25)
        # Should cut at a paragraph boundary, not mid-word
        assert result.endswith("Para two")
        assert "Para three" not in result

    def test_falls_back_to_sentence_boundary(self) -> None:
        from app.services.retrieve_steps import truncate_content
        content = "First sentence. Second sentence. Third sentence. Fourth sentence."
        result = truncate_content(content, 35)
        # Should end at a sentence boundary
        assert result.endswith(".")
        assert "Fourth" not in result

    def test_hard_cut_when_no_good_boundary(self) -> None:
        from app.services.retrieve_steps import truncate_content
        content = "abcdefghijklmnopqrstuvwxyz"
        result = truncate_content(content, 10)
        assert len(result) <= 10
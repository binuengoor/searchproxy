"""Content extraction and cleanup — strip boilerplate from raw HTML.

All raw HTML that makes it through the anti-bot firebreak (Scrape.do,
ScraperAPI) is passed through trafilatura to extract the main article text
as markdown. Already-clean markdown from Crawl4AI / Jina is left untouched
unless *aggressive* mode is enabled.
"""

from __future__ import annotations

import logging

import re

import trafilatura  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

import re

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\n{3,}|\s{2,}')

def _strip_html_fallback(text: str) -> str:
    """Best-effort HTML stripping for when trafilatura can't parse a page.
    
    Removes HTML tags, collapses excessive whitespace/newlines, and 
    strips common navigation debris patterns.
    """
    # Remove script/style blocks entirely
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.IGNORECASE | re.DOTALL)
    # Remove all remaining HTML tags
    text = _TAG_RE.sub('', text)
    # Decode common HTML entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&nbsp;', ' ').replace('&#39;', "'").replace('&quot;', '"')
    # Collapse excessive whitespace
    text = _WS_RE.sub('\n', text)
    return text.strip()



# Fragments that strongly indicate HTML rather than markdown / plain text.
_HTML_INDICATORS: tuple[str, ...] = (
    "<!doctype html",
    "<html",
    "<head",
    "<body",
    "<script",
    "<style",
    "<div",
    "<span",
    "<meta",
    "<link",
    "<iframe",
    "<nav",
    "<footer",
    "<header",
    "<section",
    "<aside",
    "<noscript",
)

# Character threshold below which we never attempt extraction — it's
# already small enough.
_CLEANUP_THRESHOLD: int = 256


class ExtractionMetrics:
    """Simple metrics holder so tests can assert reduction happened."""

    def __init__(self) -> None:
        self.raw_len: int = 0
        self.clean_len: int = 0


def _looks_like_html(content: str) -> bool:
    """Return True if *content* appears to be HTML rather than markdown/text."""
    if not content or len(content) < 20:
        return False
    normalised = content.strip().lower()[:2000]
    return any(tag in normalised for tag in _HTML_INDICATORS)



_LINK_LINE_RE = re.compile(r'\[(?:[^\]]*)\]\([^)]+\)')
_BARE_URL_RE = re.compile(r'https?://\S+')
_PIPE_NAV_RE = re.compile(r'(?:^|\n)\s*\|[^\n]*\|(?:\s*\|)+[^\n]*(?:\n|$)')
_REPEAT_DUP_RE = re.compile(r'(\n[^\n]{10,100})\1{2,}')


def _strip_markdown_spam(text: str) -> str:
    """Remove nav/menu/link-spam lines from markdown content.

    Targets patterns common in scraped pages:
    - Lines that are >70% link syntax by character count
    - Lines of pipe-delimited link tables (nav menus)
    - Repeated duplicate blocks (betting odds tables, country lists)
    - Bare URL lines
    - Collapse excessive blank lines

    This is fast regex-only — no LLM calls needed.
    """
    lines = text.split('\n')
    kept = []
    for line in lines:
        stripped = line.strip()

        # Skip empty lines (we'll re-add paragraph breaks later)
        if not stripped:
            kept.append('')
            continue

        # Skip lines that are >70% markdown link syntax
        link_chars = sum(len(m.group()) for m in _LINK_LINE_RE.finditer(stripped))
        if link_chars > len(stripped) * 0.7:
            continue

        # Skip lines that are just a bare URL
        if _BARE_URL_RE.fullmatch(stripped):
            continue

        # Skip pipe-delimited nav lines (| link | link | link |)
        if stripped.startswith('|') and stripped.count('|') >= 4 and stripped.count('](') >= 2:
            continue

        kept.append(line)

    result = '\n'.join(kept)

    # Collapse 3+ consecutive blank lines to 2
    result = re.sub(r'\n{3,}', '\n\n', result)

    # Remove repeated duplicate blocks (e.g. the same nav repeated)
    result = _REPEAT_DUP_RE.sub(r'\1', result)

    return result.strip()

def clean_content(raw: str, url: str = "", aggressive: bool = False) -> str:
    """Return agent-friendly markdown text.

    - HTML is extracted via trafilatura → clean markdown.
    - Markdown/plain text is returned unchanged unless *aggressive* is True,
      in which case trafilatura structural extraction is always run.
    - On extraction failure the original is truncated to 8 000 chars.

    Args:
        raw: Body returned by a fetch tier.
        url: Optional URL passed to trafilatura for metadata hints.
        aggressive: If True, always run trafilatura extraction even when
            the input looks like clean markdown. Use this for retrieve
            pipeline where boilerplate nav/sidebars/carts must be stripped.
            If False (default), only HTML gets structural extraction;
            markdown is returned as-is. Use this for direct /fetch calls
            where the caller wants the full page content.

    Returns:
        Cleaned string suitable for shipping to an LLM context window.
    """
    if not raw:
        return ""

    if len(raw) < _CLEANUP_THRESHOLD and not _looks_like_html(raw):
        if not aggressive:
            return raw.strip()
        # Aggressive mode: still strip link-spam even on short content
        return _strip_markdown_spam(raw.strip())

    if not aggressive and not _looks_like_html(raw):
        # Looks like markdown / plain text — no structural extraction needed.
        return raw.strip()

    # In aggressive mode with markdown input, skip trafilatura (it expects HTML)
    # but still strip markdown nav/link-spam.
    if aggressive and not _looks_like_html(raw):
        cleaned = _strip_markdown_spam(raw.strip())
        log.info(
            "Content spam-stripped %s: %d → %d chars (%.1f%% reduction)",
            url, len(raw), len(cleaned),
            (1 - len(cleaned) / len(raw)) * 100 if len(raw) > 0 else 0,
        )
        return cleaned

    try:
        extracted = trafilatura.extract(
            raw,
            url=url or None,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_images=False,
            include_links=True,
            favor_precision=True,
        )
    except Exception as exc:
        log.warning("trafilatura extraction failed for %s: %s", url, exc)
        extracted = None

    if extracted:
        # Strip markdown nav/link-spam from extracted content
        cleaned = _strip_markdown_spam(extracted.strip())
        raw_len, clean_len = len(raw), len(cleaned)
        log.info(
            "Content cleaned %s: %d → %d chars (%.1f%% reduction)",
            url,
            raw_len,
            clean_len,
            (1 - clean_len / raw_len) * 100,
        )
        return cleaned

    # Extraction failed - trafilatura could not find article content.
    # Strip HTML tags and collapse whitespace as a best-effort cleanup.
    # The quality gate (min_length) will reject truly useless pages.
    fallback_chars = 8000
    cleaned = _strip_html_fallback(raw[:fallback_chars])
    cleaned = _strip_markdown_spam(cleaned)
    log.warning(
        "trafilatura returned empty for %s, falling back to HTML+spam-stripped first %d chars (%d → %d)",
        url,
        fallback_chars,
        len(raw[:fallback_chars]),
        len(cleaned),
    )
    return cleaned
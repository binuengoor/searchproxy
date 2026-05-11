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



# Character threshold below which we never attempt extraction — it's
# already small enough.
_CLEANUP_THRESHOLD: int = 256


class ExtractionMetrics:
    """Simple metrics holder so tests can assert reduction happened."""

    def __init__(self) -> None:
        self.raw_len: int = 0
        self.clean_len: int = 0


def _strip_markdown_code_blocks(text: str) -> str:
    """Remove markdown fenced code blocks only - not indented lines.

    Stripping indented lines caused false negatives on HTML detection:
    real HTML often has 4+ space indentation, and removing those lines
    eliminated the <html>, <head>, <body> indicators that _looks_like_html
    needs. Only fenced blocks (triple backticks) are stripped, which removes
    angle-bracket mentions inside code blocks while preserving real HTML.
    """
    lines = text.split("\n")
    result: list[str] = []
    in_fenced_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fenced_block = not in_fenced_block
            continue
        if in_fenced_block:
            continue
        result.append(line)
    return "\n".join(result)


def _looks_like_html(content: str) -> bool:
    """Return True if *content* appears to be HTML rather than markdown/text.

    Strips markdown fenced code blocks before checking, so <div>/<span> inside
    ```python``` blocks don't trigger false positives.  Uses strong/weak tag
    indicators: >= 1 strong (doctype/html/head/body) or >= 2 weak tags needed.
    """
    if not content or len(content) < 20:
        return False
    text = _strip_markdown_code_blocks(content)
    normalised = text.strip().lower()[:2000]

    strong = ("<!doctype html", "<html", "<head", "<body")
    weak = ("<div", "<span", "<script", "<style", "<meta", "<link",
            "<iframe", "<nav", "<footer", "<header", "<section",
            "<aside", "<noscript")
    strong_count = sum(1 for tag in strong if tag in normalised)
    weak_count = sum(1 for tag in weak if tag in normalised)
    return strong_count >= 1 or weak_count >= 2


# ── Consent / cookie-dialog patterns ──────────────────────────────────
# GDPR cookie consent dialogs, preference centres, and privacy notice
# boilerplate that trafilatura includes in extracted text.  These usually
# appear after the real article content, but can also be the entire page
# when the source has no article content (e.g. UEFA consent-only pages).

# Heading-level triggers: if we find one of these in the last 60% of text,
# truncate from that point onward.
_CONSENT_HEADING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'^#{0,3}\s*Cookies?\s+Polic', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^#{0,3}\s*Cookies?\s+Preference', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^#{0,3}\s*Manage\s+Consent', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^#{0,3}\s*Cookie\s+Preference\s+Centre', re.IGNORECASE | re.MULTILINE),
    # UEFA-style heading: "Consent to Cookies & Data processing"
    re.compile(r'^#{0,3}\s*Consent\s+to\s+Cookies', re.IGNORECASE | re.MULTILINE),
]

# Inline / line-level triggers: individual lines that are pure consent UI noise.
# These are removed wherever they appear.
_CONSENT_LINE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'^Accept\s+All\s+Cookies?$', re.IGNORECASE),
    re.compile(r'^LET ME CHOOSE$', re.IGNORECASE),
    re.compile(r'^ONLY REQUIRED$', re.IGNORECASE),
    re.compile(r"^THAT'?S\s+OK$", re.IGNORECASE),
    re.compile(r'^Manage\s+Consent\s+Preferences?$', re.IGNORECASE),
    re.compile(r'^Cookie\s+Settings?$', re.IGNORECASE),
    re.compile(r'^Confirm\s+My\s+Choices?$', re.IGNORECASE),
    re.compile(r'^Consent\s+Leg\.?\s*Interest$', re.IGNORECASE),
    re.compile(r'^Back\s+Button$', re.IGNORECASE),
    re.compile(r'^checkbox\s+label(\s+label)+$', re.IGNORECASE),
    re.compile(r'^Apply\s+Cancel$', re.IGNORECASE),
    re.compile(r'^Search\s+Icon$', re.IGNORECASE),
    re.compile(r'^Filter\s+Icon$', re.IGNORECASE),
    re.compile(r'^Clear$', re.IGNORECASE),
    re.compile(r'^Cookie\s+List$', re.IGNORECASE),
    re.compile(r'^(Necessary|Analytical|Targeting|Functional|Marketing)\s+Cookies?$', re.IGNORECASE),
    re.compile(r'^Always\s+Active$', re.IGNORECASE),
    re.compile(r'^Allow\s+All$', re.IGNORECASE),
    re.compile(r'^Switch\s+User\s+Become\s+a\s+member$', re.IGNORECASE),
    re.compile(r'^You\s+need\s+an?\s+\w+\s+Membership\s+to\s+watch', re.IGNORECASE),
    re.compile(r'^Login\s+Create\s+account$', re.IGNORECASE),
    re.compile(r'^Disable\s+(some|all)\s+categor', re.IGNORECASE),
    # UEFA-style consent UI
    re.compile(r'^Consent\s+to\s+Cookies', re.IGNORECASE),
    re.compile(r'^Reject\s+All$', re.IGNORECASE),
    re.compile(r'^Privacy\s+settings?$', re.IGNORECASE),
]

# Block-level triggers: multi-word phrases that indicate a consent section
# has started.  We search for these in the body to find the cut point.
_CONSENT_BLOCK_TRIGGERS: list[re.Pattern[str]] = [
    re.compile(r'by clicking\b.{0,30}\bagree\b.{0,60}\bcookie', re.IGNORECASE),
    re.compile(r'you can manage\s+(?:which|your)\s+cookies?\s+(?:are\s+set|settings?)', re.IGNORECASE),
    re.compile(r'manage\s+your\s+(?:non-?essential|cookie)\s+preferences', re.IGNORECASE),
    re.compile(r'non-?essential\s+cookies?\s+(?:will\s+be\s+set|help\s+us)', re.IGNORECASE),
    re.compile(r'disable\s+(?:some|any)\s+(?:categories?\s+)?of\s+cookies', re.IGNORECASE),
    re.compile(r'these\s+cookies\s+(?:collect|are\s+essential|help\s+us)', re.IGNORECASE),
    re.compile(r'targeting\s+cookies\s+help\s+us\s+to\s+connect', re.IGNORECASE),
    re.compile(r'we\s+use\s+cookies\s+to\s+improve\s+your\s+browsing', re.IGNORECASE),
    re.compile(r'show\s+you\s+more\s+relevant\s+ads\s+online', re.IGNORECASE),
    # UEFA-style: "We, and other third parties, use cookies and other technologies"
    re.compile(r'(?:we,?\s+(?:and\s+other\s+third\s+parties,?\s+)?)?use\s+cookies\s+and\s+other\s+technologies', re.IGNORECASE),
    # "your consent is voluntary and can be withdrawn"
    re.compile(r'your\s+consent\s+is\s+voluntary\s+and\s+can\s+be\s+withdrawn', re.IGNORECASE),
    # "personal data may be shared with ... and processed by them"
    re.compile(r'personal\s+data\s+may\s+be\s+shared\s+with', re.IGNORECASE),
    # "store and/or access information on a device"
    re.compile(r'store\s+and/or\s+access\s+information\s+on\s+a\s+device', re.IGNORECASE),
    # "select personalised ads" (GDPR consent language)
    re.compile(r'select\s+personalised\s+ads\b', re.IGNORECASE),
]


def _strip_consent_dialogs(text: str) -> str:
    """Remove GDPR cookie-consent dialogs and preference-centre boilerplate.

    Strategy:
    1. Look for consent heading patterns anywhere in the text.
       If found, truncate from that heading onward — everything after is
       consent boilerplate.  These headings (Cookie Policy, Cookies Policy,
       Manage Consent Preferences, etc.) are never legitimate article content.
    2. Look for block-level trigger phrases (paragraphs about cookies,
       consent, managing preferences).  If found, truncate from that
       paragraph onward.
    3. Strip individual consent-UI noise lines wherever they appear.

    Returns cleaned text.
    """
    if not text:
        return text

    text_len = len(text)
    cutoff = text_len  # will be reduced if we find a consent section

    # ── Step 1: Heading triggers — search entire text ──
    # Consent headings like "Cookies Policy" are never legitimate article
    # content regardless of where they appear in the text.
    for pat in _CONSENT_HEADING_PATTERNS:
        m = pat.search(text)
        if m and m.start() < cutoff:
            cutoff = m.start()

    # ── Step 2: Block triggers — full-text scan for consent paragraphs ──
    # Longer phrases that reliably indicate consent boilerplate.
    for pat in _CONSENT_BLOCK_TRIGGERS:
        m = pat.search(text)
        if m and m.start() < cutoff:
            # Walk back to the start of this paragraph (previous \n\n)
            para_start = text.rfind('\n\n', 0, m.start())
            if para_start < 0:
                para_start = m.start()
            else:
                para_start += 2  # skip the \n\n itself
            if para_start < cutoff:
                cutoff = para_start

    if cutoff < text_len:
        text = text[:cutoff].rstrip()

    # ── Step 3: Strip individual consent-UI noise lines ──
    lines = text.split('\n')
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            kept.append('')
            continue
        is_consent_noise = any(
            pat.search(stripped) for pat in _CONSENT_LINE_PATTERNS
        )
        if not is_consent_noise:
            kept.append(line)

    result = '\n'.join(kept)
    # Collapse 3+ blank lines to 2
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


# ── Markdown spam stripping ────────────────────────────────────────────

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
        return _strip_consent_dialogs(_strip_markdown_spam(raw.strip()))

    if not aggressive and not _looks_like_html(raw):
        # Looks like markdown / plain text — no structural extraction needed.
        return raw.strip()

    # In aggressive mode with markdown input, skip trafilatura (it expects HTML)
    # but still strip markdown nav/link-spam.
    if aggressive and not _looks_like_html(raw):
        cleaned = _strip_consent_dialogs(_strip_markdown_spam(raw.strip()))
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
        # Strip consent dialogs first (they're at the end and large),
        # then strip markdown nav/link-spam from extracted content
        cleaned = _strip_markdown_spam(_strip_consent_dialogs(extracted.strip()))
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
    cleaned = _strip_consent_dialogs(_strip_markdown_spam(cleaned))
    log.warning(
        "trafilatura returned empty for %s, falling back to HTML+spam-stripped first %d chars (%d → %d)",
        url,
        fallback_chars,
        len(raw[:fallback_chars]),
        len(cleaned),
    )
    return cleaned
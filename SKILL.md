---
name: search-provider-orchestration
description: |
  Design and maintain self-hosted search backends that aggregate multiple providers
  (SearXNG, LiteLLM search_tools, commercial APIs) into a unified research API with
  MCP exposure. Covers provider rotation strategies, security auditing of research tools,
  and the fetch+synthesize layer that turns raw search results into Perplexity-style answers.
triggers:
  - "search provider rotation"
  - "load balance search"
  - "self-hosted search API"
  - "Perplexity alternative"
  - "research engine MCP"
  - "searchproxy"
  - "search backend simplification"
  - "third party tool"
  - "evaluate tool"
  - "security audit tool"
  - "compare research tool"
  - "crawl4ai"
---

# Search Provider Orchestration

## Overview

Self-hosted research engines typically need three layers:

1. **Provider layer** — fetch results from search APIs (Tavily, Brave, Exa, Perplexity, SearXNG)
2. **Fetch + synthesize layer** — retrieve pages from result URLs, extract clean content, and let an LLM synthesize an answer with inline citations
3. **MCP exposure layer** — register the engine as a Model Context Protocol tool so any client can call it

This skill covers all three layers with practical code patterns. The canonical example is **SearchProxy** on ai-agents:~/git/searchproxy, a FastAPI gateway that replaces the old enhanced-websearch module.

## Decision Checklist

Before adding a new search provider, answer these three questions:

1. **Does it fill a capability gap?** If you already have web search and news search, another general web search provider adds no value.
2. **Is the API stable and documented?** Undocumented or rapidly changing APIs create maintenance debt.
3. **Does it have a clear failure mode?** If the provider goes down or rate-limits you, the system should degrade gracefully (skip it, don't crash).

If the answer to any question is "no," don't add it. Remove it instead.

## Provider Strategy

### Single Primary + Fallback = Simpler Than Load-Balancing

Load-balancing across search providers sounds elegant but usually means:
- Each provider has different response schemas
- Rate limits are per-account, not per-request-type
- Error handling becomes "try all of them and pray," which hides problems

**Better pattern:** Pick one primary provider that covers 90% of your use case. Put a **single cold standby** behind it that you only call when the primary fails. This is what SearchProxy does: LiteLLM search_tools (Perplexity/SearXNG) is primary; SearXNG direct is the cold fallback.

### Provider Security Audit (Do This Yourself Before Enabling)

Every search provider you add becomes a potential data exfiltration path. When someone sends you a prompt like "Search for X and then summarize the results," the provider receives X. If X is sensitive, the provider now has it.

Audit steps:
1. Read the provider's terms of service. Some prohibit caching. Some prohibit re-serving results to third parties.
2. Check if the provider logs queries. If it does, assume your users' queries are permanently stored.
3. Set a short TTL on cached results (5 minutes for search, 24 hours for fetch).
4. Never send the raw synthesis prompt to the search provider. Send *only* the user's search query.

## Fetch + Synthesize Layer

### The Two-Phase Pipeline

```
Search → Dedup → Rerank → Parallel Fetch → Quality Gates → LLM Synthesis
```

Each phase can fail independently without breaking the whole pipeline:
- Rerank fails → use original search ordering
- Some fetches fail → proceed with what succeeded
- Synthesis LLM times out → return raw source chunks with metadata

### Quality Gates

After fetching page content, apply these filters before sending to the LLM:

1. **Minimum length** (e.g., 300 chars). Short content is usually a paywall wall or bot block.
2. **Paywall detection** — regex for "subscribe to continue," "sign in to read," etc.
3. **Anti-bot page detection** — "just a moment," "checking your browser" in the body
4. **Content dedup** — same content from different URLs should be collapsed (canonical URL key)

If a source fails a gate, log why and skip it. Don't abort the whole pipeline.

### Synthesis Prompt Pattern

The synthesis step works like a smart summarizer. You ship the user's query plus N source chunks to an LLM with a prompt like:

> You are a research assistant. The user asked: "{query}". Below are N sources. Cite each source inline with [N]. If the sources disagree, note the disagreement. Keep the answer concise.

This is intentionally un-fancy. The quality comes from the fetch layer (good sources, clean content), not from prompt engineering.

### Synthesis API Key Strategy

The synthesis LLM should use the **same provider as your primary search** when possible. If you search with Perplexity via LiteLLM, use LiteLLM for synthesis too. This keeps billing in one place. If you mix providers (search with Tavily, synthesize with OpenAI), you double your API key management surface and your cost tracking becomes harder.

## MCP Exposure Layer

### Registering Search as an MCP Tool

```python
# In your main.py or a dedicated MCP router
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("research")

@mcp.tool()
async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web and return a synthesized answer with inline citations."""
    result = await retrieve_service.retrieve(query=query, fetch_top_k=max_results)
    return result.answer
```

That's it. Any client that speaks MCP can now call your search engine without knowing anything about your internal providers.

### The MCP Surface Should Be Thin

The MCP layer should be a thin wrapper over your existing API. Don't add MCP-specific auth, rate limiting, or response formatting inside the MCP layer. Reuse what your REST API already has. This makes the MCP layer zero-maintenance.

### Crawl4AI Content Filtering Pitfalls

Crawl4AI's `/md` endpoint accepts a JSON body with filtering options. Two critical gotchas affect content cleanliness:

#### JSON key: `"f"` not `"filter"`

The endpoint expects `"f": "fit"` or `"f": "bm25"`. Sending `"filter": "fit"` is silently ignored; Crawl4AI falls back to raw mode, which returns full-page markdown including navigation bars, sidebars, and footer links.

```python
# WRONG — silently falls back to raw
body = {"url": url, "filter": "fit"}

# CORRECT — "fit" = readability extraction
body = {"url": url, "f": "fit"}
```

#### BM25 mode: no-LLM content filtering

When a user query is available, pass `"f": "bm25"` with `"q": <query>`. Crawl4AI uses BM25 keyword ranking to extract only query-relevant sections. This strips nav/ads without requiring any LLM provider credentials.

```python
# Best for /v1/retrieve pipelines  
body = {"url": url, "f": "bm25", "q": user_query}
```

| Mode | Description | Needs query | Needs provider |
|------|-------------|-------------|----------------|
| `"raw"` | Full page markdown | No | No |
| `"fit"` | Readability-style extraction | No | No |
| `"bm25"` | Keyword-ranked relevance sections | Yes | No |

**Recommendation**: For fetch pipelines that serve RAG/retrieval purposes, always use BM25 mode when a query exists. The reduction in nav/footer noise (typically 60–80% fewer characters) means downstream LLM synthesis sees only relevant text, not shopping cart links.

⚠️ **Post-processing still matters**: Even with BM25 or `fit`, some sites embed navigation as list items in the document body. An aggressive markdown cleaner (line-based heuristics for nav-link patterns, boilerplate section headings, and shop/product link URL-fragment detection like `/shop/`, `/goto/`) should be applied in the fetch pipeline for retrieval use cases.

#### Post-fetch aggressive markdown cleaner patterns

When Crawl4AI returns markdown (even with BM25/fit), navigation bars and footers can still leak through as markdown list items. Apply these three heuristics **after** fetching:

**1. Skip list-item-link lines before the first heading**

Most nav sidebars are bulleted link lists (`* [Text](URL)`) sitting above the article title. Legitimate content lists almost always appear after a heading or paragraph.

```python
before_first_heading = True
for line in lines:
    if before_first_heading and re.match(r"^#{1,6}\s+", line):
        before_first_heading = False
    if before_first_heading and re.match(
        r"^\s*[*\-]\s+\[.*?\]\(.*?\)\s*$", line
    ):
        continue  # Skip pre-heading nav link lists
```

**2. Strip boilerplate sections by heading, with subheading awareness**

When a heading like `## Apple Footer` or `## About` triggers skip mode, subheadings (`### Apple Wallet`, `### Apple Values`) must NOT exit skip mode. Only `##` or `#` headings should terminate a skip block.

```python
# Trigger skip on boilerplate heading
if re.match(r"^#{1,4}\s+", line) and _is_boilerplate_heading(line):
    skip_until_next_heading = True
    continue

if skip_until_next_heading:
    if re.match(r"^#{1,2}\s+", line):
        skip_until_next_heading = False
        if _is_boilerplate_heading(line):
            skip_until_next_heading = True
            continue
    elif re.match(r"^#{3,4}\s+", line):
        continue  # Subheadings inside a boilerplate section
    else:
        continue  # Skip non-heading lines between boilerplate blocks
    # Fall through to cleaned.append for new non-boilerplate section
```

**3. Trailing nav strip and nav-indicator lists**

Detect list links with nav URL fragments (`/shop/`, `/goto/`, `/buy_`) and nav word indicators (`home`, `about`, `contact`, `support`, `cart`, `checkout`, `subscribe`, `follow us`). Strip consecutive trailing nav lines.

```python
_NAV_PATH_FRAGMENTS = ("/shop/", "/goto/", "/buy_", "/store", "/support", "/trade_in")
_NAV_INDICATORS = ("home", "about", "contact", "privacy", "terms", "cart",
                   "checkout", "subscribe", "follow us", "shop", "buy")
```

**Why this layer is still needed:** BM25 at the Crawl4AI level filters blocks by keyword relevance, but Apple.com's nav links happen to contain product keywords (`iPhone`, `Mac`, `iPad`) that overlap with article content. Only post-fetch heuristics that recognize **structural nav patterns** (list-item links, shop URL fragments, consecutive blocks above the first heading) can catch these. This is complementary, not redundant.

**Deployment verification:** After any `docker compose build`, you **must** `docker compose up -d --force-recreate` or `stop && rm && recreate`. A plain `docker compose restart` reuses the old container image — your code changes won't be live. Verify the fix inside the container: `docker exec <container> grep -c "before_first_heading" /opt/venv/.../content_cleaner.py`.

### Aggressive Clean Mode (Retrieve vs. Fetch)

The `clean_content()` function in `content_cleaner.py` has an `aggressive` parameter:

- **`aggressive=False`** (default): Only HTML gets structural extraction via trafilatura. Already-clean markdown from Crawl4AI/Jina is returned as-is. Used by `/fetch` where the caller wants full page content.
- **`aggressive=True`**: Always run trafilatura extraction, even on markdown. Strips nav bars, sidebars, cookie banners, footer links. Used by `/v1/retrieve` where the LLM needs dense, relevant content, not navigation chrome.

**Parameter threading bug class:** When adding a parameter to `FetchChain.execute()`, it must also be threaded through `_firebreak_and_cache()` and `_firebreak()`. These private methods call `clean_content()` internally on the anti-bot firebreak path. If the parameter isn't forwarded, `NameError` fires on every anti-bot code path. The non-anti-bot paths (Crawl4AI success, Jina success) work fine because they call `clean_content()` directly from `execute()` — only the firebreak path is broken.

**Test impact:** Mock `side_effect` functions simulating `FetchChain.execute()` must accept the new keyword argument (even with a default value) or they throw `TypeError: got an unexpected keyword argument`. When adding parameters to a method that has mock side effects in tests, update all mock signatures simultaneously.

**See also:** 
- `references/crawl4ai-aggressive-clean-fix-2026-05-09.md`
- `references/consent-dialog-stripping-2026-05-10.md` — GDPR cookie consent banner removal (3-layer: heading truncation, block triggers, line removal)

### Trafilatura Extraction: favor_precision and Aggressive Fallback

When `aggressive=True` (retrieve pipeline), trafilatura's default extraction mode (`favor_recall`) keeps too much boilerplate. Switching to `favor_precision=True` tells trafilatura to prefer precision over recall, discarding marginal content blocks (sidebars, related articles, "you may also like" sections).

```python
extracted = trafilatura.extract(
    raw,
    url=url or None,
    output_format="markdown",
    include_comments=False,
    include_tables=True,
    include_images=False,
    include_links=True,
    favor_precision=True,  # Default is recall; precision reduces boilerplate
)
```

When trafilatura cannot find any article content at all (score widgets, betting pages, navigation hubs), it returns `None`. The fallback truncates to the first N chars of raw content:

```python
fallback_chars = 8000
return raw[:fallback_chars].strip()
```

**Pitfall — don't over-truncate the fallback:** An earlier version set aggressive-mode fallback to 1500 chars, hoping the quality gate (min_length=300) would reject useless pages sooner. This backfired — real article content was getting cut off mid-sentence, losing information that both the LLM and the client needed. The quality gate (300 char minimum after cleaning) already handles rejection of truly useless pages. The fallback should preserve enough content for the gate to make a good decision, not pre-filter aggressively.

Non-aggressive and aggressive modes both use 8000 chars for fallback. The difference is that aggressive mode uses `favor_precision=True` on trafilatura extraction itself, which produces cleaner output when it succeeds — the fallback is only for when extraction fails entirely.

**HTML tag stripping in the fallback:** When trafilatura extraction fails (returns `None`), the raw content is likely HTML-heavy (script blocks, nav, style sheets). The fallback path now runs `_strip_html_fallback()` which removes `<script>`/`<style>` blocks, strips all remaining HTML tags, decodes common HTML entities, and collapses whitespace before truncating to `fallback_chars`. This prevents raw HTML soup from polluting the LLM context and making the `content_length` field misleading.

**Markdown link-spam stripping (`_strip_markdown_spam`):** After content cleaning (both trafilatura success and HTML fallback), the content passes through `_strip_markdown_spam()` which removes navigation/menu/link-spam lines using pure regex — no LLM calls needed. This targets patterns common in scraped pages:

- Lines where >70% of characters are inside `[...](...)` markdown link syntax (nav bars, betting site link rows)
- Lines that are just bare URLs
- Pipe-delimited nav lines with multiple links (`| [Home](/) | [Fixtures](/fixtures) | ... |`)
- Excessive blank line collapse (3+ → max 2)
- Repeated duplicate blocks (same nav repeated at header/footer)

This runs on ALL code paths: aggressive+markdown (the `_strip_markdown_spam` early return), trafilatura extraction success (post-extraction), and HTML fallback (post-`_strip_html_fallback`). The aggressive+markdown path also uses it — content under 256 chars that's not HTML still gets spam-stripped.

**Pitfall — aggressive mode short-circuit bypasses spam stripping:** The `clean_content()` function has a short-circuit for content under `_CLEANUP_THRESHOLD` (256 chars) that returns `raw.strip()` immediately when the content doesn't look like HTML. In aggressive mode, this bypassed `_strip_markdown_spam()`. Fix: the short-circuit must check `if not aggressive` before returning early; aggressive mode must always run spam stripping even on short content.

**GDPR consent-dialog stripping (`_strip_consent_dialogs`):** After spam stripping and before truncation, run `_strip_consent_dialogs()` to remove cookie consent banners, preference centres, and privacy boilerplate. These appear after real article content (or can be the entire page for sites like UEFA that return nothing but consent dialogs). The function uses three layers:

1. **Heading truncation** — regex patterns for consent section headings (`Cookies Policy`, `Cookie Preference Centre`, `Manage Consent`, `Consent to Cookies & Data processing`). Search the ENTIRE text, not just the last 60%. These headings are never legitimate article content regardless of position. When found, truncate from that heading to end of text.

2. **Block-level triggers** — longer phrases that reliably indicate consent boilerplate (`we use cookies to improve your browsing`, `by clicking...agree...cookie`, `personal data may be shared with`, `store and/or access information on a device`, `use cookies and other technologies`, `your consent is voluntary and can be withdrawn`, `select personalised ads`). When found, walk back to the start of that paragraph and truncate from there.

3. **Line-level removal** — individual consent UI noise lines stripped wherever they appear (`Accept All Cookies`, `Confirm My Choices`, `checkbox label label`, `Apply Cancel`, `Consent Leg.Interest`, `Login Create account`, `You need an Arsenal Membership to watch`, `Reject All`, `Privacy settings`, `Necessary/Targeting/Analytical Cookies`, `Always Active`, etc.). 25+ patterns total, matching with `search()` not `fullmatch()` to handle trailing text (e.g. "Membership to watch this video").

**Pitfall — `fullmatch` vs `search` for consent line patterns:** Consent UI lines in real scraped content often have trailing text that breaks `fullmatch()` — e.g. "You need an Arsenal Membership to watch **this video**". Use `re.search()` not `re.fullmatch()` for the line-level patterns. The false-positive risk is near-zero in the searchproxy domain (sports, fixtures, research queries).

**Pitfall — consent headings at position 0:** Some pages (UEFA Champions League) return content that starts with "Consent to Cookies & Data processing" — the entire page is a consent dialog, no article content at all. Don't use a "skip first 40%" heuristic; search the full text for consent headings. These are never legitimate content.

The function runs in ALL extraction paths: before `_strip_markdown_spam()` in aggressive+markdown and trafilatura-success paths, and after `_strip_html_fallback()` in the fallback path. The order is: consent headings/blocks first (truncation), then spam lines (removal), then markdown spam (removal). Truncation first means the spam stripper has less content to process.

**Paragraph-boundary truncation (`_truncate_content`):** Instead of a hard character cutoff (`content[:max_chars]`), truncation now rounds down to the nearest paragraph boundary (`\n\n`). Falls back to sentence boundary (`. ` or `.\n`), then hard cut. This prevents mid-sentence and mid-table cuts that destroy information.

**Relevance-weighted content budget:** Instead of equal per-source caps (`total_budget / num_sources`), the `RETRIEVE_MAX_TOTAL_CONTENT` budget is distributed proportionally to `relevance_score`. A source with relevance 0.99 gets more chars than one with 0.36. Minimum floor is `budget // num_sources // 2` so even low-relevance sources aren't starved. Same total budget, smarter distribution.

**Per-source cap raised to 6000:** `RETRIEVE_MAX_CONTENT_PER_SOURCE` default increased from 4000 to 6000. Fixture/schedule pages often have 4-5K chars of genuinely useful content (tables, dates, match info) that was getting cut off at 4000.

### Source Content in API Responses: Always Return Full Content

**Always return `sources[].content` in full**, regardless of whether synthesis is enabled. Clients may need to inspect source content directly (for verification, follow-up questions, or display), and the LLM answer may not capture every detail from every source. Stripping content to save bandwidth breaks the contract — the caller expects the sources they paid to fetch.

```python
# BOTH synthesize=True AND synthesize=False:
return RetrieveResponse(
    query=query, answer=answer, citations=citations,
    sources=sources,  # full content intact
    sources_fetched=sources_fetched, sources_failed=sources_failed,
)
```

**Why we tried stripping and reverted:** An earlier optimization set `content=""` on sources when `synthesize=True` to save ~4-8KB per source. This broke client expectations — consumers want to see what the sources actually say, not just the LLM's synthesis of them. Bandwidth savings aren't worth the information loss.

**`content_length` must match `content`:** The `content_length` field in source responses should report `len(content)` *after* truncation, not the raw length before truncation. An earlier version stored the raw pre-truncation length separately, which meant `content_length: 8000` but `content` was only 4000 chars (truncated by `RETRIEVE_MAX_CONTENT_PER_SOURCE`). Always compute `content_length` from the final content string that goes into the response.

```python
# WRONG: reports raw length before truncation
raw_content_length = len(content)
content = _truncate_content(content, settings.RETRIEVE_MAX_CONTENT_PER_SOURCE)
sources.append(SourceChunk(..., content_length=raw_content_length))

# CORRECT: reports actual length after truncation
content = _truncate_content(content, settings.RETRIEVE_MAX_CONTENT_PER_SOURCE)
sources.append(SourceChunk(..., content_length=len(content)))
```

**Synthesis timeout and content size logging:** The LLM synthesis call has a 60-second timeout (increased from 30s after real queries with 5 sources × ~5KB each consistently timed out). The log line now includes total content character count so timeouts can be correlated with payload size:

```python
log.info(
    "Synthesizing answer for query='%s' with %d sources, %d total chars (model=%s, max_tokens=%d)",
    query, len(sources), total_chars, model, max_tokens,
)
```


---

## Scope Safety in Tiered Fetch Chains

### The Tiers Are: Crawl4AI → Jina Reader → (Scrape.do, ScraperAPI)

| Tier | Cost | When to use |
|------|------|-------------|
| Crawl4AI (self-hosted) | Free | Primary fetcher |
| Jina Reader | Free / cheap | When Crawl4AI returns non-anti-bot errors |
| Scrape.do / ScraperAPI | ~$0.004/page | Only when Crawl4AI/Jina hit an anti-bot block |

### Anti-Bot Escalation Rules

- **403** → always escalate to anti-bot firebreak
- **Body contains "cloudflare" / "just a moment" / etc.** → escalate
- **Timeout or 5xx** → retry once on Crawl4AI, then try Jina Reader, **never** directly to anti-bot services
- **Success but body is an anti-bot page** → escalate

This tiered approach keeps the cost floor near zero because anti-bot services are only called when truly needed.

### PDF Support via Fetch Chain

Jina Reader natively extracts text from PDF URLs. When Crawl4AI returns HTTP 500 on a PDF endpoint, the chain automatically falls back to Jina Reader, which streamed 233K characters from an arXiv PDF in testing. **No PDF-specific code is required.** The returned content is plain markdown with citations, identical to any other fetch result.

### PDF Extraction in Retrieval Flow

When using the `/v1/retrieve` endpoint with PDF URLs:
1. The fetch chain transparently routes PDFs to Jina Reader
2. Quality gates skip paywall/login PDFs (same regex patterns as web pages)
3. The synthesis layer receives clean text chunks and generates citations automatically
4. No explicit PDF handling code is needed in the orchestrator

## SearXNG Compatibility

If you use SearXNG as a search backend, the LiteLLM search_tools provider is the cleanest integration because SearXNG already has a LiteLLM-compatible JSON output. You don't need a custom SearXNG client; you just point LiteLLM at your SearXNG host with an `api_base` override.

If you need SearXNG's advanced features (images, videos, news), add a passthrough endpoint at `GET /search` that forwards query params to SearXNG and returns the raw JSON. Don't try to normalize SearXNG's schema into your own. Just proxy it.

## The "Third Party Tool" Problem

When someone asks "should we add X as a third party tool to our project?", the correct first question is: "What existing capability does it duplicate?" 

If you already have web search, adding another web search tool is not a capability gain — it's maintenance debt. The real problems to solve are:
1. **Reliability** — one search provider goes down
2. **Cost** — the current provider is too expensive
3. **Coverage** — the current provider misses a domain (e.g., academic papers)

Only add a new tool when it solves one of these three problems. Everything else is scope creep.

## Remote Code Editing Pitfalls

### Don't Use Sed for Multi-Line Python Edits on Remote Hosts

Using `sed` to modify Python code on remote hosts (via `ssh host "sed -i ..."`) is fragile for any change that spans multiple lines. Shell escaping, quote nesting, and line-by-line deletion commands produce syntax errors that are hard to debug — this session had three missing closing parentheses from sed operations on `retrieve_service.py`.

**Better pattern:** Read the file with `ssh host "cat path"`, construct the new content locally using Python string operations on the full text, validate syntax with `ast.parse()`, write the fixed file locally, then `scp` it back:

```python
# Local: read remote file, modify, validate, write back
r = terminal('ssh host "cat path/to/file.py"', timeout=10)
content = r['output']
# ... string replacement on full content ...
ast.parse(new_content)  # Validate syntax before deploying
open('/tmp/fixed.py', 'w').write(new_content)
terminal('scp /tmp/fixed.py host:path/to/file.py', timeout=10)
```

This avoids shell escaping entirely and lets you validate before deploying.

### Always Verify Syntax After Editing Remote Python

After any edit to a remote `.py` file, run `python3 -c 'import ast; ast.parse(open("file.py").read())'` on the remote host before assuming the edit succeeded. This catches missing parens, indentation errors, and other syntax issues that `sed` can introduce silently.

## Verification Steps

1. **Health check:** `curl http://localhost:8080/health` → should return `{"status":"ok"}`
2. **Search test:** `curl "http://localhost:8080/search?q=test"` → should return JSON with `results[]`
3. **Fetch test:** `curl -X POST http://localhost:8080/fetch -d '{"url":"https://example.com"}'` → should return markdown
4. **Synthesis test:** `curl -X POST http://localhost:8080/v1/retrieve -d '{"query":"test","synthesize":true}'` → should return answer with citations

## Using SearchProxy as Primary Search Source

When this agent needs web search, research, or content fetching, **use SearchProxy's REST API directly** — not the built-in `web_search` tool.

### Endpoint Selection Guide

| Need | Endpoint | Latency |
|------|----------|---------|
| Quick factual answer with citations | `POST /v1/retrieve` | 5-15s |
| Deep multi-source research report | `POST /vane` | 60-300s |
| Read a specific URL's content | `POST /fetch` | 2-10s |
| Simple search links (no synthesis) | `POST /v1/search` | 2-5s |

### Base URL

```
http://localhost:8080          # from ai-agents host
http://searchproxy.home.askbp.win   # from any machine on the LAN
```

Auth is disabled (`SEARCHPROXY_REQUIRE_AUTH=false`).

### /v1/retrieve — Primary Search Tool

Use this for most web search queries. It searches, reranks, fetches top sources, and synthesizes a cited answer.

```bash
curl -X POST http://localhost:8080/v1/retrieve \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Arsenal FC next fixture",
    "synthesize": true,
    "fetch_top_k": 5
  }'
```

Key parameters:
- `query` (required): search query
- `synthesize` (default `true`): get LLM-synthesized answer with inline `[N]` citations
- `fetch_top_k` (default `5`): number of sources to fetch and rerank
- `search_top_k` (default `10`): candidates from search before reranking

Response includes: `answer` (synthesized text), `citations[]`, `sources[]` (with `content`, `url`, `relevance_score`).

### /v1/retrieve without synthesis

For search results without LLM synthesis (just sources + content):

```bash
curl -X POST http://localhost:8080/v1/retrieve \
  -H "Content-Type: application/json" \
  -d '{"query": "Rust vs Go performance 2025", "synthesize": false}'
```

### /fetch — Read a Specific URL

When the user provides a URL or you need to read a page:

```bash
curl -X POST http://localhost:8080/fetch \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/article"}'
```

### /vane — Deep Research

For complex multi-faceted questions requiring thorough analysis:

```bash
curl -X POST http://localhost:8080/vane \
  -H "Content-Type: application/json" \
  -d '{"query": "Compare nuclear fusion investment trends across US, EU, and China", "optimization_mode": "balanced"}'
```

`optimization_mode`: `speed` (60s), `balanced` (180s), `quality` (300s).

### How to Call from Hermes

Use `terminal()` with `ssh ai-agents` and `curl`, or `web_extract` for simple fetches. For SSH-based calls:

```python
from hermes_tools import terminal
result = terminal('ssh ai-agents "curl -s -X POST http://localhost:8080/v1/retrieve -H \'Content-Type: application/json\' -d \'{\"query\":\"QUERY HERE\",\"synthesize\":true}\'"')
```

For the fetch endpoint from the local machine (if SearchProxy is reachable):

```python
result = web_extract(urls=["http://searchproxy.home.askbp.win:8080/fetch"], ...)
```

**Priority order for web search:**
1. SearchProxy `/v1/retrieve` — always use this first
2. SearchProxy `/fetch` — when you have a specific URL
3. Built-in `web_search` — only if SearchProxy is down
4. Built-in `web_extract` — only if SearchProxy `/fetch` is down

## References

- SearchProxy repo: ai-agents:~/git/searchproxy
- Crawl4AI docs: https://docs.crawl4ai.com/
- SearXNG docs: https://docs.searxng.org/
- LiteLLM search_tools: https://docs.litellm.ai/docs/completion/web_search
- MCP spec: https://modelcontextprotocol.io/
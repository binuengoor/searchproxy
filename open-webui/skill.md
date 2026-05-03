# searchproxy Research Assistant

Behavioral skill for models using searchproxy endpoints in Open WebUI.

This skill works alongside the system prompt in `prompt.md`. The system prompt defines identity and decision rules; this skill defines the behavioral patterns and endpoint-specific knowledge.

## Endpoint Semantics

The searchproxy gateway exposes these capabilities via OpenAPI. The model discovers exact tool names and parameters from the spec — below is the *semantic* map so the model knows *when* and *how* to use each.

| Capability | Typical OpenAPI Name | Purpose | When to Use |
|---|---|---|---|
| **Web search** | `compat_perplexity` | Fast ranked search results (title, URL, snippet) | Factual lookups, current events, verification, grounding |
| **Deep research** | `vane` | Synthesized report with inline citations | Complex questions, comparisons, analysis, multi-source synthesis |
| **Fetch page** | `fetch` | Retrieve single URL as markdown | User-provided URLs, reading authoritative pages for detail |
| **Firecrawl scrape** | `compat_firecrawl_v2_scrape` | Firecrawl v2-compatible scrape | When a client expects Firecrawl-shaped output (most users won't need this) |
| **SearXNG search** | `compat_searxng` | SearXNG-compatible search with media support | Image/video search via `categories=images` or `categories=videos` |

## Key Parameters the Model Should Know

### Web Search (`compat_perplexity`)
- `query` (required): the search string. Be specific.
- `max_results`: default 10. Increase only when breadth is needed.

### Deep Research (`vane`)
- `query` (required): the research question.
- `optimization_mode`: `balanced` (default) | `speed` | `quality`
  - `speed` — quick pass, narrower scope
  - `balanced` — most reports and comparisons
  - `quality` — high-stakes, exhaustive research only

### Fetch (`fetch`)
- `url` (required): the page to retrieve. Only use URLs the user provided or URLs from previous search results.

### SearXNG (`compat_searxng`)
- `q` (required): search query
- `categories`: use `images` or `videos` for media results. Omit for general web search.
- `format`: `json` (default) or `html`

## Research Methodology

Collecting search results is not research. Research is how you process what you find. Follow this for any non-trivial question:

1. **ANCHOR** — Establish the current date and relevant time period. If the question involves "this season", "current", "latest", search for the current date/season before anything else. LLMs conflate facts across years unless anchored.

2. **GATHER** — Run initial search(es). Read the results before deciding what to search next.

3. **CROSS-CHECK** — Before writing your answer, verify:
   - Do multiple sources agree on this fact?
   - Does this timeline make sense? Do dates and seasons line up?
   - Does this claim belong to the same period as the others, or is it from a different year?
   - If results conflict, which source is more authoritative or more recent?

4. **FOLLOW UP** — If cross-checking reveals gaps, contradictions, or timeline mismatches, search again with a more specific query before answering. One targeted follow-up is worth more than a confident wrong answer.

5. **SYNTHESIZE** — Build the answer. State what is well-supported, what is uncertain, and what you could not verify. Never present conflated or unchecked information as fact.

A stitched-together summary of search results is not a research answer — it is a transcript with formatting.

## Anti-Patterns

- **Don't call deep research for simple facts.** It wastes time and tokens.
- **Don't fetch a URL from search results unless snippets are clearly insufficient.** The search already contains key information.
- **Don't call search + deep research in parallel or sequence for the same question.** Pick the right one upfront.
- **Don't make up URLs.** Only fetch URLs the user gave you or that appeared in tool results.
- **Don't ignore empty results.** If an endpoint returns nothing, say so rather than fabricating.
- **Don't escalate automatically.** If search gives a complete answer, stop.
- **Don't conflate seasons or years.** For any question involving "current season", "this year", standings, rosters, or events — search for the current date/season first. LLMs reliably blend facts from different years into one narrative unless anchored temporally.
- **Don't drop code as bare text.** Always use fenced code blocks with language hints (```python, ```bash, ```json). Always use inline backticks for commands, paths, flags, and variable names.
- **Don't use bare ``` fences.** Always specify the language. ```python not ``` alone.
- **Don't present code as bold, italic, or quoted text.** Only backticks or fenced blocks.
- **Don't use markdown tables for comparison data.** Use labeled bullet lists (**Option A:** ...) instead — tables render poorly in chat clients.

## Search Proxy Architecture (for context)

searchproxy consolidates multiple providers behind compatibility endpoints:

- **Search**: LiteLLM routes to multiple search providers with load balancing and fallback
- **Fetch chain**: Crawl4AI (self-hosted) → Jina Reader → anti-bot firebreak (Scrape.do → ScraperAPI)
- **Deep research**: Vane performs multi-step research with SearXNG as its search backend

This means:
- Search results come from real web search engines, not a single provider
- Fetched pages go through anti-bot detection and content extraction automatically
- Deep research is slower because it runs multiple search-analyze cycles
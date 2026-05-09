# Design Decisions

Decisions are recorded here with rationale. If a decision is reversed, the new decision is appended with a date.

## May 2026

### Stage 3 scope: Quality + Streaming + Caching (not just caching)
**Rationale (May 2026):** The original ROADMAP had Stage 3 as "SQLite Caching Layer" only. Re-scoped because output quality improvements (better prompt, metadata, quality gates) and streaming (perceived latency) are higher-priority than caching. Caching saves money on repeated queries but doesn't improve a single response. Quality improvements make every response better. Streaming makes every response *feel* faster. Caching is still included as Stage 3c, after quality and streaming.

### Better synthesis prompt over query expansion
**Rationale (May 2026):** Query expansion (LLM expands a query into sub-queries) adds 1-3s latency and doubles LLM cost per request. The current 7-line prompt is the weakest link — it doesn't enforce citation density, doesn't scale response length to query complexity, and doesn't handle contradictions. Fixing the prompt costs zero latency and zero extra LLM calls. Query expansion is deferred until caching amortizes the cost.

### Streaming only for synthesis phase (not search/rerank/fetch)
**Rationale (May 2026):** The search → rerank → parallel fetch phases can't be meaningfully streamed because they produce structured data (result lists, relevance scores, fetched content). Only the LLM synthesis step produces a token stream. SSE events emit metadata first (query, sources, citations), then stream tokens, then a final `done` event. This matches how Perplexity works — you see the answer being typed, not the search happening.

### Content quality gates before synthesis
**Rationale (May 2026):** Sources under 300 chars after cleaning (boilerplate remnants, paywall pages, login walls) actively harm synthesis quality by polluting the prompt context. Better to drop them before LLM synthesis and let the model cite fewer but higher-quality sources than many garbage ones. 300 chars is the threshold because most legitimate web pages have at least a paragraph of content after cleaning; paywall walls and login redirects are typically under 200 chars.

### Crawl4AI gets a transient retry before Jina fallback
**Rationale (May 2026):** Crawl4AI is self-hosted and occasionally returns 502/503 on transient failures (container restart, timeout). The current code falls through to Jina on any Crawl4AI failure, but Jina can't render JavaScript. A single retry with 1s delay on 5xx/timeout catches these transient cases without adding meaningful latency to the common path. Only triggered on 5xx and timeout — not on 4xx or anti-bot blocks.

### Source metadata enrichment (fetch_tier, rerank_score, content_length, fetch_time_ms)
**Rationale (May 2026):** Clients and agents currently have zero visibility into how sources were obtained. Was it Crawl4AI (high-quality JS-rendered content) or ScraperAPI (last-resort anti-bot scraping)? How relevant is it (rerank_score)? How long did it take to fetch? This metadata enables clients to make informed decisions about source quality without any latency impact — the data is already computed, just not returned.

### Caching: SQLite in-container (not Redis)
**Rationale (May 2026):** Same decision as observability. Single-container deployment means SQLite is sufficient. Redis only makes sense if running multiple SearchProxy instances behind a load balancer. Lazy TTL expiry on read keeps the cache clean without background processes. Cache survives container restarts via Docker volume mount (same as observability.db).

### Crawl4AI plain fetch uses `/md`, not `/crawl`
**Rationale:** `/md` returns clean markdown in ~0.2s with no LLM involved. `/crawl` returns raw HTML + metadata + markdown in ~0.76s, and is only needed for structured LLM extraction. For the searchproxy `/fetch` endpoint, plain markdown is the correct primary behavior. LLM extraction is a future Phase 2+ feature.

### Keep all MCP tools; do not delete MCP endpoints even after delegating search to LiteLLM
**Rationale (May 2026, REVISED):** The previous web search service was originally built around MCP. The new `searchproxy` initially planned to expose MCP natively. This decision was reversed: SearchProxy now serves a fully dereferenced OpenAPI 3.0 spec at `/openapi.json`. MCP gateways like MCPHub auto-discover tools from this spec, eliminating the need for a custom `mcp_server.py`. This is lower maintenance and more compatible across clients.

### No framework for spec-driven development (Spec Kit, Tessl, etc.)
**Rationale:** The architecture is already defined — 4 endpoints, a fetch chain, and env vars. Writing specifications with a framework would generate maintenance artifacts (specs, plans, tasks md files) that rot over time. A single `ARCHITECTURE.md` is sufficient.

### ScrapingBee excluded from fetch chain
**Rationale:** ScrapingBee's 1,000 credits are a one-time signup bonus, not monthly recurring. Scrape.do and ScraperAPI both offer genuine monthly recurring free tiers. Scrape.do has the higher benchmark success rate and is prioritized above ScraperAPI.

### Jina Reader API key included despite free tier existing
**Rationale:** `r.jina.ai` is free with no key. Adding a key increases rate limits from 20 RPM to 500 RPM. This is a no-upside/downside decision — the key is optional and unused if absent. Future Jina integrations (Reranker, DeepSearch) will need it, so having it in the env file from day one avoids a future config scramble.

### Anti-bot services quarantined — never invoked on general fetch failures
**Rationale:** Scrape.do and ScraperAPI have limited monthly credits (1,000 each). Burning them on pages Crawl4AI or Jina can handle is wasteful. The fetch chain detects anti-bot blocks specifically (403 + Cloudflare indicators) and only then escalates to the firebreak tier.

### Crawl4AI as primary fetcher, not httpx+BeautifulSoup
**Rationale:** Crawl4AI offers JS rendering, markdown output, undetected browser + stealth modes, and structured extraction. It replaces the old httpx + BeautifulSoup + FlareSolverr stack completely and eliminates the need for a FlareSolverr container.

### No FlareSolverr
**Rationale:** Crawl4AI's undetected browser mode handles Cloudflare challenges more effectively than FlareSolverr's brute-force approach. FlareSolverr also required a separate Docker container. Removing it reduces complexity.

### No custom search provider rotation
**Rationale:** LiteLLM search router provides cross-provider load balancing (`simple-shuffle`) and automatic fallback. The old `enhanced-websearch` had ~1,250 lines of custom rotation, cooldown, and failure tracking. Delegating to LiteLLM shrinks this to ~20 lines.

### No `max_results` enforcement at the proxy layer
**Rationale:** LiteLLM passes `max_results` through to providers, but individual providers (Brave, Perplexity) may ignore it and return fixed batch sizes. The proxy normalizes responses but does not slice. Consumers that need strict limits must slice the results client-side. This is a documented behavior, not a bug.

### Image/video passthrough to upstream SearXNG
**Rationale:** LiteLLM search routers handle web search only. SearXNG supports `categories=images` and `categories=videos`. Rather than return errors or silently ignore media categories (breaking Vane's image search), we passthrough directly to an upstream SearXNG instance. If `SEARXNG_URL` is not configured, we gracefully degrade to empty `results[]`. This adds ~20 lines of code and zero operational complexity.

### Endpoint names: `/compat/perplexity`, `/vane`, `/compat/searxng`
**Rationale:** Compat endpoints are named after their external standard (`perplexity`, `searxng`). Native endpoints (`/vane`) are named after what they do. This reserves clean names (`/search`, `/research`) for future first-class implementations without breaking changes or versioning confusion.

### Open WebUI integration via OpenAPI auto-discovery, not custom tool
**Rationale:** Open WebUI's OpenAPI (Function) Server automatically discovers endpoints, parameters, and schemas from `/openapi.json`. No custom Python tool file is needed. Behavioral guidance is handled by `skill.md` + `prompt.md`, not by custom tool code.

### Force OpenAPI 3.0.3, not 3.1.0
**Rationale:** FastAPI + Pydantic v2 defaults to OpenAPI 3.1.0, which represents `Optional[str]` as `anyOf: [{type: string}, {type: null}]`. Most MCP gateways (MCPHub) and tool-calling LLM clients (Open WebUI) cannot parse `anyOf` unions — they send malformed request bodies that FastAPI rejects with 422. OpenAPI 3.0.3 renders the same types as simple `{type: string, nullable: true}` or (with concrete defaults) just `{type: string}`.

### MCPHub body-unwrap middleware
**Rationale:** MCPHub auto-generates tool schemas from OpenAPI specs and wraps the request body under a `body` key. FastAPI expects request body fields at the top level. The mismatch causes 422 for every MCPHub-originated call. A lightweight middleware detects the single-key `body` wrapper and flattens it before routing. Transparent to both direct HTTP callers and MCPHub clients.

### Observability via SQLite (in-container), not OpenObserve sidecar
**Rationale (May 2026):** OpenObserve was evaluated but rejected: (1) requires a second Docker container, violating "no external containers"; (2) HTTP JSON ingest API needs auth; (3) its search UI is query-oriented, not traffic-shaped. SQLite inside the single container is zero-config, zero auth, survives restarts via Docker volume, and the custom `/logs` HTML UI is purpose-built for HTTP exchange inspection.

### Structured logging uses correlation_id via ContextVar
**Rationale (May 2026):** LoggerAdapter requires passing the adapter through every function call. ContextVar auto-propagates across async tasks without manual threading. The CorrelationIdMiddleware sets a request-scoped UUID4 at the start of each request, and CorrelationIdFilter injects it into every log record automatically.

### Per-tier timeouts replace global FETCH_TIMEOUT
**Rationale (May 2026):** Crawl4AI and Jina typically respond in 2-5s. Anti-bot firebreak pages (Scrape.do, ScraperAPI) can take 10-30s because they run headless browsers. A single 30s timeout meant anti-bot pages succeeded but Crawl4AI/Jina had no urgency. Crawl4AI=15s, Jina=15s, AntiBot=45s gives fast tiers urgency and slow tiers headroom.

### Rerank model is user-configurable via CF_RERANK_MODEL
**Rationale (May 2026):** The BGE reranker on cf-inference supports multiple models. Hard-coding `@cf/baai/bge-reranker-base` prevents users from using larger models or future model updates. Making it a config var (`CF_RERANK_MODEL`) with a sensible default gives flexibility without requiring code changes.

### Compat endpoints hidden from OpenAPI, not deleted
**Rationale (May 2026):** `/compat/perplexity`, `/compat/searxng`, `/compat/firecrawl`, and `/v1/search` are still functional for existing clients (Open WebUI, Vane). Hiding them from the OpenAPI spec (`include_in_schema=False`) means MCPHub won't expose duplicate tools, but direct HTTP callers and existing integrations continue working unchanged. The 3-tool agent surface (retrieve, vane, fetch) is what new clients should use.
### cf-inference rerank: translate between OpenAI-style and Cloudflare-native formats
**Rationale (May 2026):** The cf-inference Worker initially accepted `documents: [...]` (OpenAI-style Cohere format) and forwarded it directly to Cloudflare Workers AI. CF expects `contexts: [{text: ""}]` and returns `response: [{id, score}]`, not `results`. This mismatch caused error 1101 on every rerank call. Rather than changing searchproxy's request format (which would break other clients), the Worker was patched to translate at the boundary: accept `documents` on the wire, forward `contexts` to CF, and map `response` back to `results` on return. This preserves OpenAI compatibility while using the native CF model.

### API key synchronization: single source of truth in cf-inference, copy to consumers
**Rationale (May 2026):** searchproxy's `.env` contained a `CF_RERANK_API_KEY` that was truncated by one character (missing final `e`) compared to the cf-inference Worker secret. This produced silent 401 Unauthorized responses from the reranker. Because searchproxy gracefully degrades when the reranker fails, the symptom was null scores — not an auth error. The fix: regenerate the key in cf-inference, copy the exact value to searchproxy `.env`, and document that cf-inference is the source of truth for its own API keys. Rotating a key means updating cf-inference secrets first, then propagating to consumers.

# SearchProxy Strategic Roadmap

> **Current version:** 0.8.0  
> **Theme:** From "search metadata relay" to "Perplexity-grade synthesis gateway"

---

## What SearchProxy Does Today

Four agent-facing endpoints (plus compat bridges):

| Tool | Endpoint | What It Does |
|---|---|---|
| **Retrieve** | `POST /v1/retrieve` | Search → BGE rerank → parallel fetch → LLM synthesis with [N] citations. Streaming with `?stream=true`. |
| **Research** | `POST /vane` | Deep research proxy to Vane/Perplexica |
| **Fetch** | `POST /fetch` | Multi-tier URL fetch: Crawl4AI → Jina → anti-bot firebreak |
| **Health** | `GET /health` | Liveness check |

Compat bridges (hidden from OpenAPI, still functional): `/compat/perplexity`, `/compat/searxng`, `/compat/firecrawl`

**What's working:** Full pipeline from search to synthesized answer with output quality gates, streaming tokens, transparent caching, and source metadata. Each step degrades independently. BGE reranker prioritizes the best sources. Parallel fetch handles anti-bot gracefully.

---

## Stage 3: Quality & Efficiency — ✅ COMPLETE (v0.8.0)

### 3a — Output Quality ✅

- Better synthesis prompt — scales answer length to query complexity, enforces citation density, handles contradictions, calls out coverage gaps
- Source metadata enrichment — `fetch_tier`, `content_length`, `rerank_score`, `fetch_time_ms` visible to clients
- Content quality gates — skip sources under 300 chars or detected paywall/login-wall

### 3b — Streaming for /v1/retrieve ✅

- SSE streaming for synthesis phase — tokens arrive in real-time, perceived latency drops significantly
- Four event types: `meta`, `source`, `token`, `done`

### 3c — Caching & Resilience ✅

- SQLite caching layer (`app/services/cache.py`) — search results (5 min TTL), fetch results (24 h TTL). Opt-in (`CACHE_ENABLED=false`). Same SQLite-in-container pattern as observability.
- Crawl4AI transient retry — one retry on 5xx/timeout before falling through to Jina

---

## Stage 4: Future Enhancements (next sprint)

| Feature | Priority | Why |
|---|---|---|
| **CI/CD** | High | GitHub Actions for test + build + deploy. Every project needs this. |
| **Query expansion** | Medium | LLM expands query into sub-queries for broader search coverage. Adds 1–3s latency — only viable because caching now amortizes repeated cost. |
| **BGE reranker on all search results** | Medium | Currently only on `/v1/retrieve`. Could add to `/compat/perplexity` as a post-processing step when `rerank=true` is passed. |
| **Redis cache** | Low | Only if running multiple SearchProxy instances behind a load balancer. SQLite is sufficient for single-instance. |
| **Rate limiting per API key** | Low | Only matters with multiple external clients. |

---

## What We Explicitly Skip

| Skip | Why |
|---|---|
| **Query intent router** | LLM clients (Open WebUI, Claude Code) already route to the right tool via tool-calling. Adding a proxy-layer router creates ambiguity. |
| **Vector DB / embedding pipeline** | Scope creep. We're a web search gateway, not a RAG platform. |
| **Firecrawl `/crawl` and `/map`** | Requires job queues, state storage, webhooks. Paradigm shift from stateless request/response. |
| **Provider-specific knobs** | Violates core architecture. LiteLLM hides provider details. |
| **LLM scoring for reranking** | Wildly expensive vs. BGE reranker. Save LLM calls for synthesis. |
| **Local file search / document RAG** | Different product. High maintenance. |
| **Feedback loops / learning** | Adds complexity without commensurate value for a self-hosted gateway. |
| **Result freshness boosting** | LiteLLM handles this via provider routing. |
| **HTML output for /fetch** | No client asking for it. Markdown is the universal agent context format. |

---

## Decision Log

| Date | Decision |
|---|---|
| 2026-05-08 | `/v1/retrieve` is the highest-value feature. Pure composition of existing search + fetch + synthesis. |
| 2026-05-08 | BGE reranker (cf-inference) used for reranking, not LLM scoring. Cheaper and purpose-built. |
| 2026-05-08 | Caching starts with SQLite in-container. Zero external services. Graduate to Redis only if multi-instance. |
| 2026-05-08 | Structured logging uses correlation_id via ContextVar. JSON formatter opt-in via `LOG_FORMAT=json`. |
| 2026-05-08 | Per-tier timeouts replace global `FETCH_TIMEOUT`. Crawl4AI/Jina = 15s, anti-bot = 45s. |
| 2026-05-08 | No query intent router at proxy layer. Tool-calling clients handle routing. |
| 2026-05-08 | OpenAPI surface cleaned to 3 agent-facing tools (retrieve, vane, fetch). Compat endpoints hidden. |
| 2026-05-09 | Stage 3 re-scoped from "caching only" to "quality + streaming + caching". Output quality is higher-priority than caching because it improves every response, not just repeated ones. |
| 2026-05-09 | Streaming for /v1/retrieve synthesis phase. Search/rerank/fetch can't stream (parallel), but LLM tokens can and should. |
| 2026-05-09 | Content quality gates skip garbage sources (<300 chars after cleaning, paywall pages) before synthesis. Better inputs = better answers. |
| 2026-05-09 | Crawl4AI gets a single transient retry on 5xx/timeout before falling through to Jina. Jina can't render JS — retrying Crawl4AI first avoids degrading SPA pages. |
| 2026-05-09 | v0.8.0 ships with 99 tests. Test count grew from 78 to 99 (+21). `test_cache.py` added 10 cache-specific tests. |
| 2026-05-09 | CI/CD deferred to Stage 4 — it is the highest priority next because every project needs automated test+build, and manual docker builds are already getting tedious. |

---

## One-Liner

> **Do not build a better search engine. Build the best gateway between the messy web and clean agent context.**

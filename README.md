# SearchProxy

Self-hosted web search gateway. Thin relay to a LiteLLM router for search, multi-tier fetch chain (Crawl4AI → Jina Reader → anti-bot quarantine), and proxy to Vane for deep research.

## What It Does

### MCP-visible tools (OpenAPI spec — what LLM models see)

| Tool | Endpoint | Purpose |
|------|----------|---------|
| **retrieve** | `POST /v1/retrieve` | One-shot research: search → BGE rerank → parallel fetch → LLM synthesis with [N] inline citations. Streaming with `?stream=true`. |
| **search_perplexity** | `POST /compat/perplexity` | Quick web search. Perplexity-compatible relay through LiteLLM. Accepts `{"query": "..."}` or full Open WebUI `messages` array (auto-extracts query) |
| **research_vane** | `POST /vane` | Deep research. Proxy to Vane with streaming support (`?stream=true`). `optimization_mode`: `speed`/`balanced`/`quality`. Accepts `messages` array (auto-extracts query) |
| **fetch_url** | `POST /fetch` | Fetch any URL as markdown. Crawl4AI → Jina Reader → anti-bot firebreak |
| **health** | `GET /health` | Liveness check |
| **metrics** | `GET /metrics` | Prometheus-style request & fetch chain metrics (no auth) |

### Runtime-only endpoints (callable but hidden from MCP discovery)

These endpoints work at runtime for backward compatibility but are excluded from the generated `/openapi.json`.

| Endpoint | Purpose |
|----------|---------|
| `/v1/search` | Alias for `/compat/perplexity` — OpenAI-style path |
| `/compat/searxng` | SearXNG JSON-compatible search. Routes web to LiteLLM, images/video to SearXNG passthrough. Supports `?format=json` (default), `?format=html`, and `?limit=N` |
| `/compat/searxng/search` | Vane-compatible subpath for SearXNG search |
| `/compat/firecrawl/v2/scrape` | Firecrawl v2-compatible scrape. Wraps the same fetch chain; accepts full Firecrawl request schema |

## Quickstart

```bash
# 1. Configure
cp .env.example .env
# Edit .env — set your LITELLM_SEARCH_URL, CRAWL4AI_URL, and optional keys

# 2. Run
docker compose up -d --build

# 3. Test
curl http://localhost:8080/health

# Quick search
curl -X POST http://localhost:8080/compat/perplexity \
  -H "Content-Type: application/json" \
  -d '{"query": "python asyncio best practices"}'

# One-shot research with synthesis (v0.8.0)
curl -X POST http://localhost:8080/v1/retrieve \
  -H "Content-Type: application/json" \
  -d '{"query": "What are the downsides of Rust async?", "max_results": 10}'

# Same, but stream tokens as they are synthesized
curl -N -X POST "http://localhost:8080/v1/retrieve?stream=true" \
  -H "Content-Type: application/json" \
  -d '{"query": "What are the downsides of Rust async?", "max_results": 10}'

# Fetch a specific URL
curl -X POST http://localhost:8080/fetch \
  -H "Content-Type: application/json" \
  -d '{"url": "https://news.ycombinator.com"}'
```

## Retrieve (`POST /v1/retrieve`)

The **one-shot research endpoint** (v0.8.0) composes the entire pipeline:

```
User query
  → LiteLLM search → raw results
  → Deduplicate by URL hash
  → BGE reranker (cf-inference) — scores and reorders by relevance
  → Parallel fetch top N URLs — Crawl4AI → Jina → anti-bot (with caching + retry)
  → Content quality gates — skip <300 chars or detected paywalls
  → LLM synthesis — structured, citation-dense answer with inline [1], [2], ...
  → Return answer + source metadata (tier, length, score, timing)
```

Request:

```json
{
  "query": "What are the downsides of Rust async?",
  "max_results": 10,
  "top_k": 8,
  "stream": false
}
```

Response (non-streaming):

```json
{
  "answer": "...",
  "citations": [
    {
      "citation_id": 1,
      "url": "https://...",
      "title": "...",
      "relevance_score": 0.91
    }
  ],
  "sources_fetched": 8,
  "sources_failed": 1,
  "source_chunks": [
    {
      "url": "https://...",
      "title": "...",
      "content": "...",
      "fetch_tier": "crawl4ai",
      "content_length": 4200,
      "relevance_score": 0.91,
      "fetch_time_ms": 820.5
    }
  ]
}
```

Streaming (`stream=true`) returns `text/event-stream`:
- `event: meta` — query, counts
- `event: source` — one per fetched source (before synthesis)
- `event: token` — LLM tokens as they arrive
- `event: done` — final metadata

## Fetch Chain

```
User → /fetch
  1. Crawl4AI (self-hosted) — fast, primary
     ├── Success → return markdown
     └── Failure
         ├── Is 5xx/timeout? — Transient retry once after 1s
         ├── Anti-bot pattern detected? → skip Jina, go to firebreak
         └── Other error → try Jina Reader
             ├── Success → return markdown
             └── Anti-bot or failure → firebreak
  2. Anti-Bot Firebreak (quarantined)
     ├── Scrape.do (attempt first)
     └── ScraperAPI (fallback)
```

Anti-bot credits are never spent on routine failures. Only confirmed Cloudflare / anti-bot blocks trigger Scrape.do / ScraperAPI.

## Configuration

All via environment variables (see `.env.example`):

**Required:**

| Variable | Note |
|----------|------|
| `LITELLM_SEARCH_URL` | Full router URL, e.g. `http://host:4000/search/unifiedsearch` |
| `LITELLM_CHAT_URL` | LiteLLM chat completions for /v1/retrieve synthesis |
| `LITELLM_CHAT_MODEL` | Model alias for synthesis, e.g. `openai/gpt-4o-mini` |

**Fetch tiers:**

| Variable | Required | Note |
|----------|----------|------|
| `CRAWL4AI_URL` | No | Primary fetch tier |
| `JINA_API_KEY` | No | 500 RPM with key, 20 RPM without |
| `SCRAPE_DO_API_KEY` | No | Only for anti-bot firebreak |
| `SCRAPERAPI_API_KEY` | No | Only for anti-bot firebreak |

**Compat / Proxy:**

| Variable | Required | Note |
|----------|----------|------|
| `SEARXNG_URL` | No | Enables image/video passthrough in SearXNG compat mode |
| `VANE_URL` | No | Needed only for `/vane` research endpoint |

**Auth & Observability:**

| Variable | Default | Note |
|----------|---------|------|
| `SEARCHPROXY_REQUIRE_AUTH` | `false` | Set `true` to require API key |
| `SEARCHPROXY_API_KEY` | — | Required when auth is enabled |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FORMAT` | `text` | `json` for structured logging |
| `OBSERVABILITY_ENABLED` | `false` | SQLite request logging |
| `OBSERVABILITY_DB_PATH` | `/data/observability.db` | SQLite path |
| `OBSERVABILITY_RETENTION_DAYS` | `7` | Auto-purge old records |

**Timeouts:**

| Variable | Default | Note |
|----------|---------|------|
| `CRAWL4AI_TIMEOUT` | `15` | Seconds |
| `JINA_TIMEOUT` | `15` | Seconds |
| `ANTIBOT_TIMEOUT` | `45` | Seconds |

**Retrieve tuning (v0.7.0+):**

| Variable | Default | Note |
|----------|---------|------|
| `RETRIEVE_MAX_RESULTS` | `10` | Search results before reranking |
| `RETRIEVE_TOP_K` | `8` | URLs to fetch after reranking |
| `RETRIEVE_MAX_CONTENT_LENGTH` | `3000` | Max chars per fetched source |
| `SYNTHESIS_MAX_TOKENS` | `2048` | Max tokens for LLM synthesis (v0.8.0) |
| `RETRIEVE_MIN_CONTENT_LENGTH` | `300` | Min chars to include a source (v0.8.0) |

**Caching (v0.8.0, opt-in):**

| Variable | Default | Note |
|----------|---------|------|
| `CACHE_ENABLED` | `false` | Set `true` to enable |
| `CACHE_SEARCH_TTL` | `300` | Search result cache TTL in seconds (5 min) |
| `CACHE_FETCH_TTL` | `86400` | Fetch result cache TTL in seconds (24 h) |
| `CACHE_DB_PATH` | `/data/cache.db` | SQLite path; mount `./data:/data` |

**Reranker (v0.7.0):**

| Variable | Default | Note |
|----------|---------|------|
| `CF_RERANK_URL` | — | Cloudflare Workers AI rerank endpoint |
| `CF_RERANK_MODEL` | `bge-reranker-base` | Rerank model name |
| `CF_RERANK_API_KEY` | — | API key for cf-inference |

## Observability

Request/response pairs are captured to an in-container SQLite database. No external services needed.

- **Browser UI:** `GET /logs` — dark-themed table, live refresh, filters, click-to-expand
- **JSON API:** `GET /api/logs?limit=20&source=searxng`
- **Retention:** Auto-purges old records every 6 hours based on `OBSERVABILITY_RETENTION_DAYS`
- **Manual clear:** **Clear All** button in `/logs`, or `DELETE /api/logs`

## Architecture

- FastAPI + httpx (async), Pydantic for validation
- Flat module structure — `app/routers/` for HTTP, `app/services/` for API clients
- Each service is independent. No service imports another.
- Config lives in `app/config.py` only. No `os.environ` scattered in code.
- Stateless. Only in-memory credit counters for Scrape.do / ScraperAPI (reset on restart).
- MCP is **not** implemented natively. Tools are exposed via the OpenAPI spec and can be consumed by any MCP gateway (e.g. MCPHub) that supports OpenAPI ingestion.

See [ARCHITECTURE.md](ARCHITECTURE.md) for full design docs, best practices, and decisions.

## Open WebUI Integration

SearchProxy exposes an OpenAPI spec at `/openapi.json`. Open WebUI auto-discovers endpoints as tools via the **OpenAPI (Function) Server** connection type. No custom tool file needed.

Setup: `open-webui/README.md`

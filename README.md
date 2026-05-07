# SearchProxy

Self-hosted web search gateway. Thin relay to a LiteLLM router for search, multi-tier fetch chain (Crawl4AI → Jina Reader → anti-bot quarantine), and proxy to Vane for deep research.

## What It Does

### MCP-visible tools (OpenAPI spec — what LLM models see)

| Tool | Endpoint | Purpose |
|------|----------|---------|
| **search_perplexity** | `POST /compat/perplexity` | Quick web search. Perplexity-compatible relay through LiteLLM. Accepts `{"query": "..."}` or full Open WebUI `messages` array (auto-extracts query) |
| **research_vane** | `POST /vane` | Deep research. Proxy to Vane with streaming support (`?stream=true`). `optimization_mode`: `speed`/`balanced`/`quality`. Accepts `messages` array (auto-extracts query) |
| **fetch_url** | `POST /fetch` | Fetch any URL as markdown. Crawl4AI → Jina Reader → anti-bot firebreak |
| **health** | `GET /health` | Liveness check |

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

curl -X POST http://localhost:8080/compat/perplexity \
  -H "Content-Type: application/json" \
  -d '{"query": "python asyncio best practices"}'

curl -X POST http://localhost:8080/fetch \
  -H "Content-Type: application/json" \
  -d '{"url": "https://news.ycombinator.com"}'
```

## Fetch Chain

```
User → /fetch
  1. Crawl4AI (self-hosted) — fast, primary
     ├── Success → return markdown
     └── Failure
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

| Variable | Required | Note |
|----------|----------|------|
| `LITELLM_SEARCH_URL` | Yes | Full router URL, e.g. `http://host:4000/search/unifiedsearch` |
| `CRAWL4AI_URL` | No | Primary fetch tier |
| `JINA_API_KEY` | No | 500 RPM with key, 20 RPM without |
| `SCRAPE_DO_API_KEY` | No | Only for anti-bot firebreak |
| `SCRAPERAPI_API_KEY` | No | Only for anti-bot firebreak |
| `SEARXNG_URL` | No | Enables image/video passthrough in SearXNG compat mode |
| `VANE_URL` | No | Needed only for `/vane` research endpoint |
| `SEARCHPROXY_REQUIRE_AUTH` | No | Default `false` |
| `SEARCHPROXY_API_KEY` | No | Required when auth is enabled |
| `LOG_LEVEL` | No | `DEBUG`, `INFO` (default), `WARNING`, `ERROR` |
| `OBSERVABILITY_ENABLED` | No | Enable SQLite request logging. Default `false` |
| `OBSERVABILITY_DB_PATH` | No | SQLite path. Default `/data/observability.db` |
| `OBSERVABILITY_RETENTION_DAYS` | No | Auto-purge records older than N days. Default `7` |

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

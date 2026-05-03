# SearchProxy

Self-hosted web search and content fetch gateway. Thin relay to LiteLLM search routers, multi-tier fetch chain with anti-bot quarantine, and proxy layer for Vane deep research.

## Purpose

- **Search:** Relay queries through a LiteLLM search router for cross-provider load balancing.
- **Fetch:** Retrieve web page content through a priority chain: self-hosted Crawl4AI → Jina Reader → anti-bot specialists (Scrape.do, ScraperAPI) reserved only for Cloudflare-protected sites.
- **Research:** Transparent proxy to Vane deep-research service.
- **Compat:** Bridge to SearXNG JSON format for tools and scripts expecting that schema.

## Design Constraints

- Self-hosted. No SaaS APIs for core search (delegate to LiteLLM router).
- Low-maintenance. Single config file. No manual provider lists or rotation logic.
- Security-first. No `curl | bash`. No hard-coded secrets. Optional API keys only for external fallbacks.
- Anti-bot credits quarantined. Scrape.do (1,000/mo) and ScraperAPI (1,000/mo) are **never** used for normal fetches. Only when Crawl4AI + Jina both fail on a known anti-bot block (403 with Cloudflare indicators).

## Stack

```
Python 3.11+
FastAPI
httpx (async HTTP client)
Pydantic (validation)
```

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/search` | POST | Thin relay to LiteLLM search router. Normalizes query + max_results. |
| `/v1/search` | POST | Alias for `/search` (OpenAI-compatible naming). |
| `/compat/searxng` | GET | Convert SearXNG query params → LiteLLM search → normalize to SearXNG JSON. |
| `/research` | POST | Proxy request to Vane deep-research endpoint. Passthrough. |
| `/fetch` | POST | Fetch a single URL. Runs Crawl4AI → Jina Reader → anti-bot firebreak. |

## Environment Configuration

```bash
# --- Search ---
LITELLM_SEARCH_URL=http://upstream-host:4000/search/unifiedsearch

# --- Research (Vane) ---
VANE_URL=http://upstream-host:3001
VANE_CHAT_PROVIDER_ID=29a86a6a-721c-414f-bb0b-67a4f5a2d8fc
VANE_CHAT_MODEL_KEY=opencode-go/mimo-v2-omni
VANE_EMBED_PROVIDER_ID=481e7ec6-873e-4e8d-ad58-e49b214d8729
VANE_EMBED_MODEL_KEY=text-embedding-3-small

# --- Fetch: Crawl4AI (self-hosted) ---
CRAWL4AI_URL=http://localhost:11235

# --- Fetch: Jina Reader (free tier; unlimited, optional key for higher limits) ---
JINA_API_KEY=

# --- Fetch: Anti-bot specialists (only for Cloudflare/anti-bot blocks) ---
SCRAPE_DO_API_KEY=
SCRAPERAPI_API_KEY=
```

All keys are optional. If missing, the associated fetch tier is simply skipped.

## Fetch Chain

```
User requests /fetch?url=<URL>
│
▼
1. Crawl4AI (self-hosted, primary)
   ├── Success → return markdown + metadata
   └── Failure
       ├── Is 403 / Cloudflare / anti-bot indicator?
       │   → Skip Jina (it can't bypass anti-bot)
       │   → Go directly to Anti-Bot Firebreak
       │
       └── Other error (5xx, timeout, DNS)
           → Go to Jina Reader
           │   ├── Success → return markdown
           │   └── Failure / Is anti-bot block?
           │       → Go to Anti-Bot Firebreak
           │
▼
2. Jina Reader (free cloud backup)
   Only reached for general failures (not anti-bot blocks)
   Returns clean markdown for most pages
│
▼
3. Anti-Bot Firebreak (quarantined credits)
   Only reached for confirmed anti-bot blocks
   Priority: Scrape.do → ScraperAPI
   Never used for routine fetches
│
▼
Error (all tiers exhausted)
```

### Anti-Bot Detection

A response is treated as an anti-bot block if any of these are true:
- HTTP status is 403
- Response body contains known indicators: `cloudflare`, `just a moment`, `checking your browser`, `ddos-guard`
- Crawl4AI returns an explicit anti-bot error

### Rate Limit Philosophy

| Tier | Strategy |
|------|----------|
| Crawl4AI | No limit (self-hosted) |
| Jina Reader | 20 RPM without key, 500 RPM with key |
| Scrape.do | 1,000/month — tracked client-side to prevent overage |
| ScraperAPI | 1,000/month — tracked client-side to prevent overage |

Monthly counters for paid tiers reset on calendar-month boundaries.

## Project Layout

```
searchproxy/
├── ARCHITECTURE.md          ← This file
├── CHANGELOG.md             ← Release notes
├── .cursorrules             ← AI agent context (optional)
├── .gitignore
│
├── app/
│   ├── __init__.py
│   ├── main.py              ← FastAPI app, lifespan, middleware
│   ├── config.py            ← Pydantic Settings from env
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── search.py        ← /search, /v1/search, /compat/searxng
│   │   ├── research.py      ← /research (Vane proxy)
│   │   └── fetch.py         ← /fetch (multi-tier chain)
│   └── services/
│       ├── __init__.py
│       ├── litellm_search.py
│       ├── searxng_compat.py
│       ├── vane_proxy.py
│       ├── crawl4ai.py
│       ├── jina_reader.py
│       ├── scrape_do.py
│       ├── scraperapi.py
│       └── fetch_chain.py   ← Orchestrates tiers + anti-bot detection
│
├── tests/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── project/                 ← Gitignored working directory
    ├── TODO.md              ← Current tasks and progress
    ├── DECISIONS.md         ← Design decisions and rationale
    └── CHANGELOG_WORKING.md ← Draft release notes
```

## MCP Server (Phase 2)

After the HTTP API is stable, an `mcp_server.py` can expose the same endpoints through the Model Context Protocol via stdio or SSE. The MCP layer will import the same `services/` modules and call the same business logic.

## Decisions

| Decision | Rationale |
|----------|-----------|
| No custom provider rotation | LiteLLM router handles this. We deleted 1,250 lines from the old codebase. |
| No `max_results` enforcement at proxy | LiteLLM passes it through, individual providers may ignore it. Client-side slicing if strict limits needed. |
| Scrape.do before ScraperAPI | 98% success vs 61%. Higher credit efficiency. |
| Jina Reader included even though `r.jina.ai` is free | With API key, rate limits go from 20 RPM → 500 RPM. Future-proofing for Jina Reranker etc. |
| No FlareSolverr | Crawl4AI's undetected browser + stealth modes handle Cloudflare better than brute-force headless. |
| No `000-index.md` or hub files | Navigates via file explorer / quick switcher per project convention. |

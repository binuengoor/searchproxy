# SearchProxy

Self-hosted web search and content fetch gateway. Thin relay to LiteLLM search routers, multi-tier fetch chain with anti-bot quarantine, and proxy layer for Vane deep research.

## Purpose

- **Fetch:** Retrieve web page content through a priority chain: self-hosted Crawl4AI → Jina Reader → anti-bot specialists (Scrape.do, ScraperAPI) reserved only for Cloudflare-protected sites.
- **Vane:** Transparent proxy to Vane deep-research service.
- **Compat:** Compatibility bridges for external API formats:
    - `/compat/perplexity` — Perplexity/OpenAI-style search responses (thin relay to LiteLLM).
    - `/compat/searxng` — SearXNG JSON format. Supports optional image/video passthrough to upstream SearXNG.

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
| `/compat/perplexity` | POST | Thin relay to LiteLLM search router. Perplexity-compatible responses. |
| `/v1/search` | POST | Alias for `/compat/perplexity` (OpenAI-compatible naming). |
| `/compat/searxng` | GET | SearXNG JSON compatibility. Image/video passthrough to upstream SearXNG when configured. |
| `/vane` | POST | Deep research proxy to Vane. Input: query + optional depth/breadth. Output: synthesized report with inline citations. Supports streaming (`?stream=true`). |
| `/fetch` | POST | Fetch a single URL. Runs Crawl4AI → Jina Reader → anti-bot firebreak. |
| `/compat/firecrawl/scrape` | POST | Firecrawl v2-compatible scrape. Wraps the same fetch chain; accepts full Firecrawl request schema. Unsupported params are accepted and ignored. |

## Environment Configuration

```bash
# --- Core: API Authentication ---
SEARCHPROXY_API_KEY=change-me-in-production

# --- Compat: Perplexity / OpenAI search ---
# LiteLLM search router endpoint. Must include the full router name.
LITELLM_SEARCH_URL=http://litellm-host:4000/search/unifiedsearch

# --- Vane deep research ---
VANE_URL=http://vane-host:3001
VANE_CHAT_PROVIDER_ID=
VANE_CHAT_MODEL_KEY=
VANE_EMBED_PROVIDER_ID=
VANE_EMBED_MODEL_KEY=

# --- Compat: SearXNG passthrough (optional; enables image/video categories) ---
SEARXNG_URL=http://searxng-host:8980

# --- Fetch: Crawl4AI (self-hosted) ---
# Use /md for plain markdown fetch (fast, no LLM involved)
# Use /crawl with extraction_config only for structured LLM extraction
CRAWL4AI_URL=http://crawl4ai-host:11235

# Optional: LLM config for Crawl4AI structured extraction (not plain fetch)
# Only needed if using Crawl4AI's LLM extraction features
CRAWL4AI_LLM_PROVIDER=openai/gpt-4o-mini
CRAWL4AI_LLM_BASE_URL=http://litellm-host:4000/v1
CRAWL4AI_LLM_API_KEY=sk-your-secret-key

# --- Fetch: Jina Reader (optional key for higher rate limits) ---
# No key = 20 RPM. With key = 500 RPM + access to Reranker/DeepSearch
JINA_API_KEY=

# --- Fetch: Anti-bot specialists (quarantined) ---
SCRAPE_DO_API_KEY=
SCRAPERAPI_API_KEY=
```

All keys are optional. If missing, the associated fetch tier is simply skipped.

## Fetch Chain

**API Standard:** `/fetch` behaves like a standard web page fetcher — input is a URL, output is markdown/plain text with metadata. Comparable to `r.jina.ai/http://<URL>` or `firecrawl.dev`. Supports `?format=markdown|text|html`.

### API Standards

| Endpoint | Standard Comparable To | Input | Output |
|----------|------------------------|-------|--------|
| `/compat/perplexity` | Perplexity API, OpenAI `/v1/search` | `{"query": "...", "max_results": 10}` | `{"results": [...]}` with title, url, snippet |
| `/compat/searxng` | SearXNG JSON API (`?format=json`) | Query params: `q`, `categories`, `engines`, etc. | Standard SearXNG JSON with `results`, `answers`, `suggestions`, `infoboxes` |
| `/vane` | Vane, Perplexity, Jina DeepSearch | `{"query": "...", "depth": "balanced"}` | Synthesized report with inline citations. Streams when `?stream=true` |
| `/fetch` | `r.jina.ai`, Firecrawl | `{"url": "https://..."}` or `?url=...` | Markdown/text + metadata |

### Image/Video Passthrough for `/compat/searxng`

SearXNG supports `categories=images` and `categories=videos`, but LiteLLM search routers handle web search only.

**Behavior:**
- `categories=general` (default) or any unrecognized category: route through LiteLLM, return web results in SearXNG format.
- `categories=images`, `categories=videos`, or engines like `bing images`, `youtube`: if `SEARXNG_URL` is configured, passthrough directly to the upstream SearXNG instance. Return raw SearXNG JSON (including `results[]` with image/video URLs, thumbnails, etc.).
- If `SEARXNG_URL` is not set, return empty `results[]` for media categories. This is graceful degradation — clients see zero results, not an error.

This keeps `/compat/searxng` functionally equivalent to a real SearXNG instance for all verticals Vane already uses. The passthrough is ~20 lines of code and adds zero operational complexity.

```
User requests POST /fetch {"url": "https://example.com"}
```

```
User requests /fetch?url=<URL>
│
▼
1. Crawl4AI (self-hosted, primary)
   Endpoint: POST /md for plain markdown fetch (fast, clean)
   Endpoint: POST /crawl with extraction_config only for structured LLM extraction (slower)
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
│   │   ├── search.py        ← /compat/perplexity, /v1/search
│   │   ├── searxng.py       ← /compat/searxng
│   │   ├── vane.py          ← /vane (deep research proxy)
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
├── open-webui/
│   ├── skill.md             ← Behavioral guidance: when to call each endpoint
│   ├── prompt.md              ← System prompt preset for Native/Agentic Mode
│   └── README.md              ← Setup guide: OpenAPI connection + skill attachment
└── project/                   ← Gitignored working directory
    ├── TODO.md              ← Current tasks and progress
    ├── DECISIONS.md         ← Design decisions and rationale
    └── CHANGELOG_WORKING.md ← Draft release notes
```

## MCP Server (Phase 2)

SearchProxy does not expose a native MCP server. Instead, it serves a fully dereferenced OpenAPI 3.0 spec at `/openapi.json`. This allows any MCP gateway that supports OpenAPI ingestion (e.g. MCPHub) to auto-discover and route tools without a custom MCP implementation.

**MCPHub configuration example:**
```yaml
# tools.yaml snippet
- name: searchproxy
  type: openapi
  url: https://searchproxy.home.askbp.win/openapi.json
```

This approach replaces the need for a dedicated `mcp_server.py` module.

## Best Practices

These rules shape how the code is written. They exist to keep the codebase readable, safe to refactor, and easy to change after six months of not looking at it.

### 1. Flat Is Better Than Nested
- No deeply nested packages. `app/services/*` is one level deep. That's it.
- No `app/core/utils/helpers/` indirection. If it's a helper, it lives next to the only thing that uses it or in `app/utils.py` if shared.
- No premature abstraction: if there's only one implementation, there is no interface or abstract base class. Add one only when a second real variant exists.

### 2. One Level of Abstraction Per Function
- A function either **orchestrates** (calls other functions, handles flow) OR **does work** (makes an HTTP call, parses text, formats output). Never both.
- Example: `fetch_chain.execute()` orchestrates. `crawl4ai.fetch()` does work. `crawl4ai.fetch()` must not call `jina_reader.fetch()`.

### 3. Configuration at the Boundary
- **Only `app/config.py` reads environment variables.** No `os.environ.get()` anywhere else.
- Settings are injected: routers and services receive a `settings` argument, never import a global.
- Secret values (API keys) live in `.env` only. They never appear in `ARCHITECTURE.md`, tests, or logs.

### 4. Routers Are Thin
- A router handles input validation, calls a service, and returns the response. That's it.
- Target: every router endpoint under 20 lines.
- No business logic in routers. No retries in routers. No logging decisions in routers.

### 5. Services Are Independent HTTP Clients
- Each service (`crawl4ai.py`, `jina_reader.py`, etc.) is a standalone module that knows:
  - How to build a request for its target
  - How to validate/transform the response
  - The failure modes it produces
- Each service owns its own `httpx.AsyncClient` or accepts a shared session. It does not reach into another service.

### 6. Explicit Error Handling
- Never swallow exceptions silently. If a fetch tier fails, return a result object with an error field — not `None`.
- Use typed result objects: `FetchResult(success=False, error="Cloudflare block detected", status=403, content="")`
- Log once at the decision point, not in every inner function.
- HTTP status codes are the contract:
  - `502` = downstream service (LiteLLM, Vane, Crawl4AI) failed
  - `429` = rate limited by our own credit tracker or Jina
  - `503` = all fetch tiers exhausted
  - `400` = client sent bad input
  - `500` = unexpected crash (should be rare)

### 7. Structured Logging Only
- No `print()` anywhere. Use the standard `logging` module with a JSON formatter in production.
- Every log line includes: `correlation_id`, `endpoint`, `method`, `latency_ms`.
- Log decisions, not noise. "Escalating to Scrape.do due to Cloudflare block" is a decision. "Got response" is noise.

### 8. Async Everything
- Every I/O operation is `async`. No blocking `requests` calls, no `time.sleep()`, no synchronous file reads.
- Use `asyncio.gather()` only when operations are genuinely parallel and independent.
- Timeouts on every outbound request. Default: 15s for search, 30s for fetch.

### 9. Type Hints Everywhere
- Every function parameter and return value has a type hint.
- Use `from __future__ import annotations` (Python 3.11) for forward references.
- Pydantic models for request/response bodies. Don't use raw `dict` for API contracts.

### 10. Testable Without Mocking Frameworks
- Services accept `httpx.AsyncClient` as a constructor argument. Tests pass a custom client that returns recorded responses.
- No `unittest.mock.patch`. Patching breaks refactoring. Dependency injection lets you swap the client.
- Tests live in `tests/` and mirror the `app/` structure: `tests/services/test_crawl4ai.py` tests `app/services/crawl4ai.py`.

### 11. Minimal Dependencies
- Every package in `pyproject.toml` must justify its existence.
- FastAPI + uvicorn + httpx + pydantic are the core. No ORM, no DB driver, no Redis client.
- `python-dotenv` only in dev. Production loads env via Docker.

### 12. Stateless
- The service holds no in-memory state that survives a request.
- Exception: monthly credit counters for Scrape.do / ScraperAPI are stored in a simple in-memory dict. On restart, counters reset. Over-spend risk is one extra request after restart — acceptable for free tiers, not for paid.
- No in-memory caches. If caching is needed, use HTTP cache headers or add a caching proxy later.

### 13. Consistent Response Shape
- All successful responses are the resource the endpoint promises.
- All error responses share this shape:
  ```json
  {
    "detail": "Human-readable error message",
    "error_code": "FETCH_ANTI_BOT_EXHAUSTED",
    "correlation_id": "uuid"
  }
  ```

## Best Practices

These rules keep the codebase readable and safe to refactor six months from now. If a rule is broken, fix it immediately — don't "come back later."

### 1. No Clever Code
- Explicit over implicit. `if is_anti_bot: return anti_bot_fetch(url)` beats a ternary chain.
- If it needs a comment to explain the *what*, rewrite it.
- Comments explain the *why* only. The *what* must be obvious from the code.

### 2. One Thing Per Module
- A module either **fetches from one API** or **routes requests**. Never both.
- `services/crawl4ai.py` calls Crawl4AI. `services/fetch_chain.py` orchestrates. `fetch_chain.py` never reaches into `httpx` directly.
- If a module is over 150 lines, it is too big. Split it.

### 3. No Premature Abstraction
- If there is only one implementation, there is no interface and no base class.
- No `BaseFetcher` with three overridden methods. Just three simple functions.
- Add abstraction when a second real alternative exists, not when you imagine one might.

### 4. Flat Over Nested
- Max directory depth: `app/services/crawl4ai.py` (3 levels). Not `app/core/domain/services/fetch/crawl4ai.py`.
- Flat is searchable. Deep is discoverable only by the person who built it.

### 5. Configuration at the Boundary
- **Only `app/config.py` reads environment variables.** No `os.environ.get("KEY")` anywhere else.
- Pydantic Settings is the single source of truth for all config.
- `.env` is the only file with secrets. `.env` is gitignored. Never put keys in code, tests, or docs.

### 6. Explicit Error Handling
- Never swallow exceptions silently. If a fetch fails, surface it.
- Use typed result objects: `FetchResult(success=False, error="Cloudflare block", status=403)`.
- Log once at the decision point, not in every inner function.

### 7. Structured Logging Only
- No `print()` anywhere. Use the standard `logging` module with an optional JSON formatter.
- Every log line includes a `request_id`. Use `logging.LoggerAdapter` for this.
- Log *decisions*, not noise. `"Escalating to anti-bot for github.com"` is useful. `"Got 200 from Jina"` is noise.

### 8. Async Everything
- All I/O is `async`. No `requests.get()`, no `open()`, no `time.sleep()`.
- Use `asyncio.gather()` only for genuinely parallel, independent calls.
- Every outbound request has a timeout. Default: 15s for search, 30s for fetch.

### 9. Type Hints Everywhere
- Every function signature has type hints. Every public function has a return type.
- Use Pydantic models for request/response bodies. No `dict[str, Any]` across module boundaries.
- Use `from __future__ import annotations` (Python 3.11) for forward reference support.

### 10. Testable Without Patching
- Services accept `httpx.AsyncClient` as a constructor argument or function parameter.
- Tests pass a custom client that returns recorded responses.
- No `unittest.mock.patch`. Patching breaks refactoring. Dependency injection keeps tests stable.

### 11. Routers Stay Thin
- A router does three things: validate input, call a service, return the response.
- Target: every endpoint handler under 20 lines.
- No business logic in routers. No retry loops. No logging configuration.

### 12. Stateless
- The service holds no in-memory state that survives a request.
- Exception: monthly credit counters for Scrape.do / ScraperAPI are stored in a simple in-memory dict. On restart, counters reset. This is a conscious tradeoff.
- No in-memory caching. If caching is needed, use an external layer.

### 13. Consistent Response Shape
- All errors share this exact format:
  ```json
  {
    "detail": "Human-readable error",
    "error_code": "FETCH_ANTI_BOT_EXHAUSTED",
    "request_id": "uuid"
  }
  ```
- Success responses are whatever the endpoint promises — no wrapping, no `"data"` envelope.

### 14. One Concept Per Function
- A function either **orchestrates** (calls other functions) or **does work** (makes an HTTP call, parses, formats). Never both.
- `fetch_chain.execute()` orchestrates. `crawl4ai.fetch()` does work.

### 15. Minimal Dependencies
- Every package in `pyproject.toml` must justify its existence.
- Core: FastAPI, uvicorn, httpx, pydantic. That's it.
- No ORM, no DB driver, no Redis client at this stage.

## Decisions

| Decision | Rationale |
|----------|-----------|
| No custom provider rotation | LiteLLM router handles this. We deleted 1,250 lines from the old codebase. |
| No `max_results` enforcement at proxy | LiteLLM passes it through, individual providers may ignore it. Client-side slicing if strict limits needed. |
| Scrape.do before ScraperAPI | 98% success vs 61%. Higher credit efficiency. |
| Jina Reader included even though `r.jina.ai` is free | With API key, rate limits go from 20 RPM → 500 RPM. Future-proofing for Jina Reranker etc. |
| No FlareSolverr | Crawl4AI's undetected browser + stealth modes handle Cloudflare better than brute-force headless. |
| No `000-index.md` or hub files | Navigates via file explorer / quick switcher per project convention. |

## Open WebUI Integration

Open WebUI discovers searchproxy tools automatically via its **OpenAPI (Function) Server** connection type. No custom Python tool file is needed. The model receives endpoint signatures, parameter types, and descriptions directly from the auto-generated `/openapi.json` spec.

### Connection Setup

Admin Panel → Settings → Connections → Add Connection → OpenAPI (Function) Server:
- **URL:** `http://<searchproxy-host>:<port>/openapi.json`
- **Auth:** Bearer token via `Authorization: Bearer <SEARCHPROXY_API_KEY>` header
- searchproxy auto-generates the spec from FastAPI; all endpoints appear as auto-discovered tools

### Supporting Files (`open-webui/`)

Three files guide the model's *behavior*, not its plumbing:

| File | Purpose |
|------|---------|
| `skill.md` | Defines *when* to call which endpoint: quick lookup (`/compat/perplexity`), deep research (`/vane`), or fetch a URL (`/fetch`). |
| `prompt.md` | System prompt preset establishing the model's identity as a research assistant using searchproxy endpoints. |
| `README.md` | User-facing setup guide: connect the OpenAPI server, attach the skill, and enable Native/Agentic Mode. |

**No custom tool file is maintained.** OpenAP I provides full auto-discovery of endpoints, parameters, and return schemas.

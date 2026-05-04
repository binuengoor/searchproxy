# Changelog

All notable changes to SearchProxy will be documented in this file.

## [0.4.0] — Unreleased

### Added (Tool consolidation — OpenAPI/MCP surface)
- **Reduced visible endpoints from 7 to 3 (+ health)** — Hidden redundant aliases from OpenAPI spec so MCPHub/Open WebUI only exposes canonical tools:
  - `POST /v1/search` → hidden (`include_in_schema=False`) — alias of `/compat/perplexity`
  - `GET /compat/searxng` → hidden — SearXNG consumers (Vane) call it directly
  - `GET /compat/searxng/search` → hidden — Vane subpath alias
  - `POST /compat/firecrawl/v2/scrape` → hidden — Firecrawl-native SDKs call it directly
  - Endpoints still work at runtime; only excluded from the generated `/openapi.json`
- **Capability-focused descriptions** on all visible tools — Replaced mechanical summaries ("Perplexity-compatible search") with "when to use" guidance:
  - `search_perplexity` — "Quick web search for facts and lookups"
  - `research_vane` — "Deep research with synthesis and citations"
  - `fetch_url` — "Fetch content from a specific URL"
- **Spec size reduced from ~88 KB to ~17 KB** — Fewer endpoints = less context bloat in the LLM system prompt

### Fixed (OpenAPI spec correctness + LLM consumability)
- **`/v1/search` endpoint actually exists** — Previously documented in README but absent from code. Added `POST /v1/search` as a real alias to `/compat/perplexity` with the same request/response schema.
- **`/compat/searxng` 200 response now shows `SearxngResponse` schema** — Previously returned empty `{}` due to `response_model=None` and union return types. Now properly documents all response fields: `query`, `number_of_results`, `results[]` (with `title`, `url`, `content`, `engine`, `score`, `category`, and `extra='allow'` fields), `answers`, `corrections`, `suggestions`, `infoboxes`, `unresponsive_engines`.
- **`/compat/firecrawl/v2/scrape` 200 response now shows `FirecrawlResponse` schema** — Previously returned `additionalProperties: true` (untyped dict). Now properly documents: `success` (boolean), `data` (with `markdown`, `html`, `metadata`), `error` (string on failure).
- **`/vane` `optimization_mode` parameter now has `enum`** — Spec now explicitly lists `"speed"`, `"balanced"`, `"quality"` with timeout descriptions. MCP clients and Open WebUI can now show a dropdown instead of a free-text field.
- **`/fetch` `format` query param description clarified** — Now reads: "Response format: markdown (default; currently the only supported format). Future: text, html." Eliminates ambiguity about what the parameter does.
- **All endpoints now accept Open WebUI's full Perplexity request shape** — Models (Open WebUI, Claude Code, etc.) often send the full Perplexity/ChatGPT payload including `messages`, `model`, `stream`, `return_related_questions`, `search_recency_filter` to every search/research endpoint. Instead of 422 errors:
  - `PerplexityQuery` (used by `/compat/perplexity` and `/v1/search`) now accepts `messages` array and extracts `query` from the last `user` message.
  - `VaneRequest` (used by `/vane`) now also accepts `messages` array and extracts `query` from the last `user` message.
  - All other Perplexity fields (`model`, `stream`, `return_related_questions`, `search_recency_filter`) are accepted and silently ignored.
  - This prevents 422 errors when LLM clients send their standard chat-completion-shaped payloads.
- **`/compat/searxng` and `/compat/searxng/search` now accept `limit` parameter** — LLM clients almost always hallucinate a `limit` parameter when calling search APIs. Added `limit` as an alias for `max_results` with the same `ge=1, le=100` constraints. Passed through to LiteLLM search.
- **All endpoints now have explicit `operation_id`** — Clean, readable IDs for MCP tool naming:
  - `search_perplexity` → `POST /compat/perplexity`
  - `search_v1` → `POST /v1/search`
  - `search_searxng` → `GET /compat/searxng`
  - `search_searxng_vane` → `GET /compat/searxng/search`
  - `research_vane` → `POST /vane`
  - `fetch_url` → `POST /fetch`
  - `scrape_firecrawl` → `POST /compat/firecrawl/v2/scrape`
  - `health` → `GET /health`
- **`/health` endpoint now has a typed `HealthResponse` schema** — Previously returned `dict[str, str]` which produced an empty object schema in OpenAPI. Now properly documents `{ "status": "ok" }`.

### Architecture
- All response schemas in OpenAPI now match actual runtime types. No more `dict[str, Any]` or `response_model=None` hiding schemas from spec consumers.
- Consistent pattern: union-return handlers (JSON vs HTML) use `responses={200: {"model": ...}}` annotations to expose JSON schema while keeping runtime flexibility.
- `model_validator` on `PerplexityQuery` and `VaneRequest` extracts query from `messages` at validation time — no runtime changes needed in service layer.

## [0.1.0] — Unreleased

### Added
- Initial project scaffold: FastAPI app, routers, services layer.
- `POST /compat/perplexity` — thin relay to LiteLLM search router with normalized request/response.
- `POST /v1/search` — OpenAI-compatible alias for `/compat/perplexity`.
- `GET /compat/searxng` — convert SearXNG query params to LiteLLM search (web), normalize response to SearXNG JSON.
- `GET /compat/searxng/search` — Vane-compatible subpath alias for `/compat/searxng`.
- `GET /compat/searxng?format=html` — HTML response mode for browser consumption (simple results page).
- Media passthrough: `categories=images` or `categories=videos` forwarded directly to upstream SearXNG (JSON + HTML).
- `POST /vane` — transparent proxy to Vane deep-research service.
- `POST /fetch` — multi-tier fetch chain.
  1. Crawl4AI (self-hosted, primary)
  2. Jina Reader (cloud backup for general failures)
  3. Anti-bot firebreak with detection logic — Scrape.do → ScraperAPI (quarantined for Cloudflare blocks only)
- Anti-bot detection: HTTP 403, body indicators (`cloudflare`, `just a moment`, `checking your browser`, `ddos-guard`), explicit Crawl4AI errors.
- Monthly client-side credit tracking for Scrape.do and ScraperAPI to prevent overage on free tiers.
- Pydantic Settings from environment variables with sensible defaults.
- Docker + docker-compose setup for self-hosting.
- `GET /` root redirect to `/docs` for zero-config browser-based API testing and phone health checks.
- Comprehensive ARCHITECTURE.md with design constraints, endpoint matrix, fetch chain diagram, and decision rationale.

## [0.3.0] — Unreleased

### Added
- `app/services/content_cleaner.py` — trafilatura-based HTML boilerplate removal.
  - Detects HTML vs markdown via tag sniffing; extracts article text as clean markdown.
  - Passes through already-clean markdown unchanged.
  - Falls back to truncated raw HTML (8 000 char cap) on extraction failure.
- Content cleaning wired into `FetchChain.execute()` on **every success path**:
  - Crawl4AI → cleaned
  - Jina Reader → cleaned
  - Scrape.do → cleaned
  - ScraperAPI → cleaned
- Demo: `https://blog.cloudflare.com` fetched via anti-bot firebreak
  - raw HTML: **109,733 chars** → cleaned: **~374 chars** (99.6% reduction)
- `trafilatura>=2.0.0` added to `pyproject.toml` dependencies.
- `tests/test_content_cleaner.py` — 11 tests covering extraction, pass-through, fallback, and HTML detection.

### Fixed
- `/fetch` no longer returns raw undifferentiated HTML from anti-bot services. Previously the agent context was flooded with scripts, navbars, cookie banners, and inline SVG when the firebreak activated.
- **SearXNG image/video passthrough stripped media metadata** — `img_src`, `thumbnail_src`, `resolution`, `source`, and other upstream SearXNG fields were discarded due to strict manual field mapping in `SearxngResult`. Fixed by enabling `extra="allow"` on the model and forwarding all raw fields during passthrough. HTML mode now renders thumbnails for media results.

## [0.2.0] — 2025-05-03

### Added
- `POST /compat/firecrawl/v2/scrape` — Firecrawl v2-compatible scrape endpoint.
  - Thin wrapper around existing `/fetch` chain (Crawl4AI → Jina → anti-bot firebreak).
  - Accepts full Firecrawl request schema (`url`, `formats`, `timeout`, `actions`, `location`, etc.).
  - Unsupported params accepted and logged as ignored.
  - Returns Firecrawl-shaped JSON: `{"success": true, "data": {"markdown": "...", "metadata": {...}}}`.
- `app/services/firecrawl_compat.py` — pure formatting mapper, no HTTP calls.
- `app/routers/firecrawl.py` — thin router following existing convention (<100 lines).
- `tests/test_firecrawl.py` — 6 tests covering success, failure, ignored params, missing url, auth required, auth rejected.

### Architecture
- No custom search provider rotation — delegated entirely to LiteLLM router.
- No FlareSolverr — Crawl4AI's undetected browser + stealth modes replace brute-force headless.
- No manual provider lists or ranking logic — service is a thin gateway, not a router.
- Anti-bot services isolated: never invoked for routine fetches, only for confirmed anti-bot blocks.
- Jina Reader included with optional API key: enables higher rate limits (500 RPM vs 20 RPM) and future Jina services (Reranker, Embeddings).

## Planned

### 0.2.0
- ~~MCP server layer (`mcp_server.py`) exposing tools via stdio/SSE~~ — **Replaced by OpenAPI spec ingestion via MCPHub**. The fully dereferenced OpenAPI 3.0 spec at `/openapi.json` is the integration point; no native MCP server needed.
- Health check and metrics endpoints (`/health`, `/metrics`).
- Structured logging with correlation IDs across async requests.
- Add `max_results` client-side slicing after LiteLLM response normalization.
- Graceful degradation when Crawl4AI container is unreachable (skip to Jina instead of error).

### 0.3.0
- Jina Reranker integration for `/search` response post-processing.
- Request/response caching with TTL for identical search queries.
- Configurable fetch chain depth via query param.


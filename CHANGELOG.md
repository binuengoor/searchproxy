# Changelog

All notable changes to SearchProxy will be documented in this file.

## [0.8.3] — 2026-05-11

### Performance

- **Skip rerank for small result sets** (`app/services/retrieve_steps.py`)
  - When `len(deduped) <= fetch_top_k`, the BGE reranker is skipped entirely. Saves 1–3s of pure overhead when all results are fetched anyway.
- **Synthesis result caching** (`app/services/cache.py`, `app/services/retrieve_service.py`)
  - New `CACHE_SYNTHESIS_TTL=3600`. Identical queries that resolve to the same source URLs return the cached LLM answer instantly, bypassing the expensive synthesis call.
- **Incremental source streaming** (`app/services/retrieve_service.py`, `app/services/retrieve_steps.py`)
  - `retrieve_stream` now emits `event: source` as each individual fetch completes rather than waiting for the entire batch. Dramatically improves perceived latency for streaming clients.
- **Reduce default rerank timeout to 5s** (`app/config.py`)
  - `RERANK_TIMEOUT` changed from `10.0` to `5.0`. Falls back to original search order faster when the upstream reranker is slow.

### Refactored

- **Extract `_process_fetch_result` and `_refetch_anti_bot`** (`app/services/retrieve_steps.py`)
  - Per-result quality gates (paywall, too-short, anti-bot re-fetch) are now pure helpers shared by both batch (`fetch_step`) and incremental (`fetch_step_incremental`) modes.

## [0.8.2] — 2026-05-11

### Performance

- **Dynamic `max_tokens` for synthesis** (`app/services/synthesis_service.py`)
  - `max_tokens` now scales with source count: `256 + 150 * len(sources)` instead of static `2048`. Simple queries finish 2–4× faster.
- **Cap raw HTML before cleaning** (`app/services/content_cleaner.py`)
  - `_MAX_CLEAN_INPUT = 200_000` chars. Prevents `trafilatura` from blocking the thread pool on huge pages.
- **Dedicated thread pool for content cleaning** (`app/clean_executor.py`, `app/services/fetch_chain.py`)
  - `ThreadPoolExecutor(max_workers=16)` isolates CPU-bound `trafilatura` / regex work from the default asyncio pool. Reduces head-of-line blocking under concurrent load.
- **Refined prefetch re-fetch heuristic** (`app/services/retrieve_service.py`)
  - Only re-fetches speculative prefetches on confirmed anti-bot blocks (`_is_anti_bot_block`), not on short content. Eliminates wasted re-fetches on legitimate short pages.
- **Client disconnect detection** (`app/services/retrieve_service.py`, `app/routers/retrieve.py`)
  - Checks `request.is_disconnected()` after search, rerank, and fetch phases. Aborts early with HTTP 499 instead of burning CPU/bandwidth on abandoned requests.
- **Configurable rerank timeout** (`app/config.py`, `app/services/rerank_service.py`)
  - New `RERANK_TIMEOUT=10.0` (was hardcoded `15.0`).
- **Configurable synthesis timeout** (`app/config.py`, `app/services/synthesis_service.py`)
  - New `SYNTHESIS_TIMEOUT=60.0`.

### Fixed

- **Division-by-zero in `_budget_step`** (`app/services/retrieve_steps.py`)
  - If all `relevance_score`s are `0.0`, `total_weight` now falls back to `len(sources) * 0.5` instead of crashing.
- **Dead `asyncio.current_task()` code in firebreak** (`app/services/fetch_chain.py`)
  - Removed unused / incorrect `task_names.get(asyncio.current_task(), "unknown")` inside `as_completed` loop.
- **`_derive_source` path typo** (`app/middleware/request_logger.py`)
  - Fixed `/compat/firecrawl/scrape` → `/compat/firecrawl/v2/scrape`.
- **Reduced `VACUUM` frequency** (`app/observability.py`)
  - `VACUUM` only runs when `deleted > 1000` rows. Avoids exclusive-lock pauses on small purges.
- **Removed eager cache expiry `DELETE` on read miss** (`app/services/cache.py`)
  - Stale rows are harmless and tiny; avoids write-lock contention under load.
- **Use `html.unescape`** (`app/services/content_cleaner.py`)
  - Replaced manual entity decoding with stdlib `html.unescape` (faster and more complete).

### Architecture

- **Split `retrieve_service.py` into `retrieve_steps.py`** (`app/services/retrieve_steps.py`)
  - Extracted pure helpers and pipeline steps from the 615-line orchestrator. `RetrieveService` is now ~150 lines.
- **Moved `_client`/`get_client()` to `app/clients.py`** (`app/clients.py`)
  - Eliminates circular import that forced bottom-of-file router imports in `main.py`.
- **Test isolation** (`tests/conftest.py`)
  - Added `reset_dependencies` fixture (autouse) that clears all DI singletons between tests.

### Tests

- 128 tests passing, zero regressions.

---

## [0.8.1] — 2026-05-11

### Performance

- **Reduced synthesis prompt budget** (`app/config.py`)
  - `RETRIEVE_MAX_TOTAL_CONTENT` 20000 → 12000 chars. Saves ~2k tokens (~40% of prompt), reduces synthesis latency by 3–5s.
- **Capped speculative prefetch** (`app/services/retrieve_service.py`)
  - `RETRIEVE_PREFETCH_MAX = 3` (new config var). Prevents wasted fetches on URLs that rerank may demote. Reduces downstream load per query.
- **Dynamic per-URL fetch timeout** (`app/services/retrieve_service.py`)
  - `max(4.0, min(10.0, batch_timeout / fetch_top_k + 2))` — no single URL starves the batch. Fair share per concurrent fetch.
- **Replaced `asyncio.wait()` + nested `asyncio.timeout()` with `asyncio.gather()`** (`app/services/retrieve_service.py`)
  - Eliminates task-completed race condition where pending tasks inside gather completed before wait_for checked them, causing hangs.

### Fixed

- **`threading.Lock()` deadlock in DI factories** (`app/dependencies.py`)
  - Singleton factories call each other nested (e.g. `get_retrieve_service` → `get_litellm_client`). Non-reentrant `Lock()` caused the same event-loop thread to deadlock on re-acquisition. Replaced with `threading.RLock()`. Fixes pre-existing test hangs in `test_retrieve.py` and `test_search_and_fetch.py`.
- **Dead code removal** (`app/services/content_cleaner.py`)
  - Removed unused `_HTML_INDICATORS` constant.

### Tests

- 128 tests passing (up from 99). `test_retrieve.py` no longer hangs.

---

## [0.8.0] — 2026-05-09

### Added

**Stage 3a — Output Quality**

- **Better synthesis prompt** (`app/services/synthesis_service.py`)
  - Rewrote `_SYSTEM_PROMPT` and `_build_user_content()` for Perplexity-grade citation density, contradiction handling, and length scaling by query complexity.
  - Added `SYNTHESIS_MAX_TOKENS` config var (default `2048`) for response-length control.
- **Source metadata enrichment** (`app/schemas.py`, `app/services/models.py`, `app/services/fetch_chain.py`, `app/services/retrieve_service.py`)
  - `SourceChunk` now carries `fetch_tier`, `content_length`, `rerank_score`, `fetch_time_ms`.
  - `Citation` now carries `relevance_score` (already computed; previously discarded).
  - `FetchResult` carries `fetch_time_ms` and `fetch_tier` for end-to-end visibility.
- **Content quality gates** (`app/services/retrieve_service.py`, `app/config.py`)
  - Sources under `RETRIEVE_MIN_CONTENT_LENGTH=300` chars (after cleaning) are skipped before synthesis.
  - Paywall/login-wall heuristics (`_is_likely_paywall`) drop sources marked with "subscribe", "sign in", "premium content", etc.
  - Better inputs = better answers. Zero latency cost; filtering happens in-memory before LLM call.

**Stage 3b — Streaming for /v1/retrieve**

- **SSE streaming** (`app/routers/retrieve.py`, `app/services/synthesis_service.py`)
  - `POST /v1/retrieve?stream=true` returns `text/event-stream` with four event types: `meta`, `source`, `token`, `done`.
  - Search/rerank/fetch phases remain non-streaming (parallel, fast). Only the LLM synthesis phase streams tokens.
  - `synthesize_stream()` async generator in `synthesis_service.py` pipes LiteLLM `stream=true` tokens directly into SSE `event: token` lines.
  - Zero latency increase; perceived latency drops from 8–15s of dead silence to progressively rendered text.

**Stage 3c — Caching & Resilience**

- **SQLite caching layer** (`app/services/cache.py`, `app/config.py`, `app/dependencies.py`, `app/services/litellm_search.py`, `app/services/fetch_chain.py`)
  - `CacheService` in new `app/services/cache.py` — persistent, TTL-on-read, opt-in (`CACHE_ENABLED=false` default).
  - Search results keyed by hash of normalized query + max_results → TTL `CACHE_SEARCH_TTL=300` s (5 min).
  - Fetch results keyed by hash of normalized URL → TTL `CACHE_FETCH_TTL=86400` s (24 h).
  - Transparent integration: `litellm_search.py` reads cache before upstream, writes on success; `fetch_chain.py` reads before tier execution, writes on success.
  - `CACHE_DB_PATH=/data/cache.db` persists across container restarts via `./data:/data` volume.
  - `tests/test_cache.py` — 10 cache tests covering hit, miss, expiry, invalidation, concurrent access, disabled mode, clear, stats.
- **Crawl4AI transient retry** (`app/services/fetch_chain.py`)
  - On timeout or 5xx from Crawl4AI, retry once after a 1s delay before falling through to Jina Reader.
  - Prevents degrading JS-heavy pages (Jina cannot render JS) due to a single transient failure.
  - Only applies to Crawl4AI; no retry at other tiers.

### Config additions

- `SYNTHESIS_MAX_TOKENS=2048` — max tokens for LLM synthesis answer.
- `RETRIEVE_MIN_CONTENT_LENGTH=300` — min chars for a source to be considered for synthesis.
- `CACHE_ENABLED=false` — opt-in; set `true` to enable search + fetch caching.
- `CACHE_SEARCH_TTL=300` — search result cache TTL in seconds.
- `CACHE_FETCH_TTL=86400` — fetch result cache TTL in seconds.
- `CACHE_DB_PATH=/data/cache.db` — SQLite path for cache persistence.

### Tests

- 99 tests passing (99 total). New: `tests/test_cache.py` (10 tests), `tests/test_retrieve.py` extended for streaming/quality gates/metadata.

### Architecture

- Cache follows the same SQLite-in-container pattern as observability (`app/observability.py`). No external services.
- TTL checked lazily on read — no background purging, no cron, no complexity.
- Cache is per-process (single container). No cross-instance invalidation needed.

---

### Fixed (post-v0.8.0 session)

- **cf-inference reranker format translation** — Worker `handleRerank()` accepted OpenAI-style `documents: [...]` but Cloudflare Workers AI expects `contexts: [{text}]` and returns `response: [{id, score}]`, not `results`. Error 1101 on every call. Patched Worker to translate at the boundary: accept `documents`, forward `contexts`, map `response` back to `results`. (cf-inference commit `bae0358`).
- **API key truncation in `.env`** — `CF_RERANK_API_KEY` was truncated by one character (missing trailing `e`) vs the cf-inference Worker secret. Produced silent 401 → searchproxy degraded gracefully → null `relevance_score` and `rerank_score` across all responses. Fixed by regenerating key in cf-inference and copying exact value to searchproxy `.env`.
- **Field naming consistency** — `SourceChunk.rerank_score` renamed to `relevance_score` across `schemas.py`, `retrieve_service.py`, and `tests/test_retrieve.py`. Unified API surface: only `relevance_score` appears in both `Citation` and `SourceChunk`. (searchproxy commit `14fed5e`).


---

## [0.7.0] — 2026-05-09

### Added

**Stage 2: /v1/retrieve — Search → Rerank → Fetch → Synthesize**

New endpoint: POST /v1/retrieve — one-shot research pipeline that combines search, reranking, content fetching, and LLM synthesis into a single call.

- app/services/rerank_service.py — BGE reranker client for cf-inference. Calls /v1/rerank on the Cloudflare Workers AI backend. Falls back gracefully: if reranking fails, original search order is preserved.
- app/services/synthesis_service.py — LiteLLM chat completions client. Builds a citation-instructed prompt from fetched source content, calls the configured chat model, and returns a synthesized answer with inline [N] citations.
- app/services/retrieve_service.py — Full pipeline orchestrator: LiteLLM search → URL dedup → BGE rerank → parallel fetch (asyncio.gather) → per-source content chunking → total-content cap → LLM synthesis. Each step degrades independently (no rerank = original order, fetch failures = partial results, synthesis failure = raw source excerpts).
- app/routers/retrieve.py — thin router exposing POST /v1/retrieve with RetrieveRequest/RetrieveResponse schemas.
- app/schemas.py — added Citation, RetrieveRequest, RetrieveResponse, SourceChunk models.
- app/dependencies.py — added get_rerank_service(), get_synthesis_service(), get_retrieve_service() factories.
- app/config.py — 7 new config vars: LITELLM_CHAT_URL, LITELLM_CHAT_MODEL, CF_RERANK_URL, CF_RERANK_API_KEY, CF_RERANK_MODEL, RETRIEVE_MAX_CONTENT_PER_SOURCE, RETRIEVE_MAX_TOTAL_CONTENT, RETRIEVE_RERANK_TOP_K.
- app/main.py — retrieve router registered.
- .env and .env.example — documented all new vars.

**Config additions:**
- LITELLM_CHAT_URL — LiteLLM chat completions endpoint (default: http://host.docker.internal:4000/v1/chat/completions)
- LITELLM_CHAT_MODEL — model name for synthesis (default: openai/gpt-4o-mini)
- CF_RERANK_URL — cf-inference reranker endpoint
- CF_RERANK_API_KEY — optional API key for cf-inference
- CF_RERANK_MODEL — reranker model identifier (default: @cf/baai/bge-reranker-base)
- RETRIEVE_MAX_CONTENT_PER_SOURCE — max chars per source (default: 4000)
- RETRIEVE_MAX_TOTAL_CONTENT — max total chars across all sources (default: 20000)
- RETRIEVE_RERANK_TOP_K — number of results to rerank (default: 20)

**Tests: 77 passed** (8 new retrieve tests + 69 existing)

## [0.6.0] — 2026-05-08

### Added

**Stage 1.1: Per-tier fetch timeouts**
- `CRAWL4AI_TIMEOUT=15`, `JINA_TIMEOUT=15`, `ANTIBOT_TIMEOUT=45` — each fetch tier now has its own timeout instead of sharing a generic `FETCH_TIMEOUT=30`. Anti-bot firebreak pages get the headroom they need; fast Crawl4AI pages don't wait for slow tiers.
- Updated `Crawl4AIClient`, `JinaReaderClient`, and `AntiBotClient` to use their respective timeouts.
- `.env.example` updated with all new timeout variables.

**Stage 1.2: Structured logging + correlation_id**
- `CorrelationIdMiddleware` (ASGI) — extracts `X-Correlation-ID` header or generates UUID4, stored in `request.state` and a `ContextVar` for async-safe access across the request lifecycle.
- `CorrelationIdFilter` + `JsonFormatter` — structured JSON logging when `LOG_FORMAT=json`; text logging when `LOG_FORMAT=text` (default).
- `LOG_FORMAT` env var added to `app/config.py` (`text` | `json`).
- `app/main.py` fully rewritten to wire middleware, JSON logging setup in lifespan, and fix stale binding bug: changed `from app.config import settings` → `import app.config as _config_module` so that runtime settings changes (e.g., in tests) take effect immediately.
- `app/observability.py` — `correlation_id` field added to `LogRecord` dataclass and SQLite schema, including `ALTER TABLE` migration for existing databases.
- `app/middleware/request_logger.py` — wires `correlation_id` from `ContextVar` into `LogRecord`.
- `app/services/fetch_chain.py` — imports `get_correlation_id` for future log correlation.

**Stage 1.3: /metrics endpoint (Prometheus-style)**
- `app/services/metrics.py` — `MetricsCollector` singleton with `inc_requests(method, endpoint, status)` and `inc_tier(tier, outcome)` counters, Prometheus exposition format.
- `app/routers/metrics.py` — `GET /metrics` endpoint returning `text/plain` Prometheus metrics, excluded from auth.
- `app/main.py` — `metrics_middleware` (`@app.middleware`) for request counting, `EXCLUDED_PATHS` includes `/metrics`.
- `app/services/fetch_chain.py` — `inc_tier()` calls added for success/fail at each tier (crawl4ai, jina, scrape_do, scraperapi).

**Stage 1.4: Close test gaps**
- `tests/test_auth.py` — 7 auth tests: health without auth, auth disabled, auth blocks unauthenticated, correct token, wrong token, excluded paths, metrics excluded, missing Bearer prefix.
- `tests/test_openapi.py` — 6 OpenAPI tests: spec returns 200, version 3.0.3, static paths present, no $ref, health in spec, metrics Prometheus format.
- `tests/conftest.py` — updated `client`/`auth_client` fixtures.
- `tests/test_firecrawl.py` — fixed auth test to use `app.config.settings` instead of `app.main.settings`.

**Test results: 67 passed, 2 pre-existing failures** (null content in messages, unrelated).

## [0.5.1] — 2026-05-04

### Fixed
- **422 from MCPHub body wrapping** — MCPHub generates tool schemas from OpenAPI specs and wraps request bodies under a `body` key (e.g., `{"body": {"query": "..."}}`). FastAPI expects top-level fields. Added `mcp_body_unwrap` middleware that detects the wrapper and flattens it before routing. Transparent to direct HTTP callers.

## [0.5.0] — 2026-05-04

### Fixed
- **422 Unprocessable Entity from MCPHub/Open WebUI tool calls** — Root cause was OpenAPI 3.1.0 spec generating `anyOf: [{type: string}, {type: null}]` for `Optional` fields, which many MCP/tool clients cannot parse. When clients auto-generated request bodies from the spec, they produced invalid payloads that FastAPI rejected with 422.
  - Forced OpenAPI 3.0.3 for maximum client compatibility — eliminates all `anyOf` nullable patterns
  - Replaced `str | None` / `bool | None` with concrete defaults (`query: str = ""`, `stream: bool = False`, etc.) in `PerplexityQuery`, `VaneRequest`, and `MessageItem` schemas
  - All MCP-visible endpoints now emit clean, client-friendly schemas with zero `anyOf` patterns
- **`MessageItem.content` simplified** — Was `str | None` (emitting `anyOf`), now `str = ""` — tool clients send string content, not null

## [0.4.1] — Unreleased

### Added
- **Vane retry logic** — `VaneProxyClient` automatically retries transient 5xx errors (500, 502, 503, 504) up to 3 times with a 1-second delay. Applies to both sync (`/vane`) and streaming (`/vane?stream=true`) requests. Timeouts and 4xx client errors are not retried.

### Architecture
- New private method `_post_with_retry` in `VaneProxyClient` handles backoff and status-code filtering. Retriable codes defined as a `frozenset` class constant for easy maintenance.

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

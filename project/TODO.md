# SearchProxy — TODO & Progress

> Active work tracker. Completed items live in CHANGELOG.md and git history.

---

### Stage 1: Tactical Debt — ✅ COMPLETE (v0.6.0)

- [x] Per-tier fetch timeouts (Crawl4AI=15s, Jina=15s, AntiBot=45s)
- [x] Correlation ID middleware + structured JSON logging (LOG_FORMAT=json)
- [x] /metrics endpoint (Prometheus format, MCP-visible, auth-free)
- [x] Auth + OpenAPI test suites (13 tests total)
- [x] Fix: settings binding from `from app.config import settings` → `import app.config as _config_module`
- [x] Fix: MessageItem.content `str | None` — null content from tool-call messages no longer 422s

---

### Stage 2: /v1/retrieve — ✅ COMPLETE (v0.7.0)

- [x] Config: LITELLM_CHAT_URL, LITELLM_CHAT_MODEL, CF_RERANK_*, RETRIEVE_* tuning vars
- [x] app/services/rerank_service.py — BGE reranker via cf-inference, graceful fallback
- [x] app/services/synthesis_service.py — LiteLLM chat with citation-instructed prompt
- [x] app/services/retrieve_service.py — full pipeline: search → dedup → rerank → parallel fetch → chunk → synthesize
- [x] app/routers/retrieve.py — thin router with rich OpenAPI description
- [x] app/schemas.py — Citation, RetrieveRequest, RetrieveResponse, SourceChunk
- [x] app/dependencies.py — factory functions for all new services
- [x] app/main.py — retrieve router registered, correlation middleware, JSON logging
- [x] OpenAPI surface cleaned to 3 agent-facing tools (retrieve, vane, fetch)
- [x] Compat endpoints hidden from OpenAPI (include_in_schema=False)
- [x] .env + .env.example — all new vars documented
- [x] tests/test_retrieve.py — 8 tests (success, empty, rerank fallback, fetch failure, no-synthesize, 422, dedup, partial failure)
- [x] All 78 tests passing

---

### Stage 3: Quality & Efficiency — ✅ COMPLETE (v0.8.0)

#### 3a — Output Quality ✅

- [x] Better synthesis prompt — structured, citation-dense prompt; length scales to query complexity
- [x] Source metadata enrichment — `fetch_tier`, `content_length`, `relevance_score`, `fetch_time_ms` in SourceChunk; `relevance_score` in Citation
- [x] Content quality gates — skip sources under 300 chars or detected paywall/login-wall before synthesis

#### 3b — Streaming for /v1/retrieve ✅

- [x] SSE streaming — `stream=true` returns `text/event-stream` (events: meta, source, token, done)
- [x] `synthesize_stream()` async generator pipes LiteLLM tokens into SSE lines

#### 3c — Caching & Resilience ✅

- [x] SQLite caching layer (`app/services/cache.py`) — transparent search + fetch caching, TTL on read
- [x] Crawl4AI transient retry — one retry on 5xx/timeout before falling through to Jina

#### 3d — Polish ✅

- [x] Better Citation prompt via synthesis prompt work (rolled into 3a)
- [x] Rerank score in response via metadata enrichment (rolled into 3a)
- [x] Documentation update — ROADMAP, CHANGELOG, ARCHITECTURE, TODO for v0.8.0

---

---

### v0.8.2 — Code Review Implementation (2026-05-11)

**Performance:**
- [x] Dynamic `max_tokens` for synthesis (scales with source count)
- [x] Cap raw HTML to 200KB before content cleaning
- [x] Dedicated thread pool (`clean_executor.py`) for CPU-bound trafilatura/regex work
- [x] Refined prefetch re-fetch heuristic — only on anti-bot blocks, not short content
- [x] Client disconnect detection in `/v1/retrieve` pipeline (HTTP 499 abort)
- [x] Configurable rerank timeout (`RERANK_TIMEOUT=10.0`)
- [x] Configurable synthesis timeout (`SYNTHESIS_TIMEOUT=60.0`)

**Bugs:**
- [x] Fix division-by-zero in `_budget_step` when all relevance scores are 0.0
- [x] Remove dead `asyncio.current_task()` code in anti-bot firebreak loop
- [x] Fix `_derive_source` path typo (`/v2/scrape`)
- [x] Reduce `VACUUM` frequency — only when deleted > 1000 rows
- [x] Remove eager cache expiry `DELETE` on read miss (avoids write-lock contention)
- [x] Use `html.unescape` instead of manual entity decoding

**Maintainability:**
- [x] Split `retrieve_service.py` (615 lines) into `retrieve_service.py` + `retrieve_steps.py`
- [x] Move `_client`/`get_client()` to `app/clients.py` (eliminates circular import)
- [x] Add `reset_dependencies` autouse fixture in `conftest.py`
- [x] Update `.env.example` with all new config vars
- [x] 128 tests passing, zero regressions

---

### Known Issues / Limitations

- `/vane` with deep research queries can take 2+ minutes (Vane backend timeout, not searchproxy)
- `/vane` streaming requires client-side SSE handling
- `/compat/searxng` image/video passthrough returns count: 0 when SearXNG has no results

### Deferred (after Stage 3)

- Query expansion via LLM (adds 1–3s latency; needs caching first to amortize cost)
- BGE reranker on all search results (not just /v1/retrieve)
- Redis cache (only if multi-instance)
- Rate limiting per API key
- Firecrawl `/crawl` and `/map` compatibility
- CI/CD (GitHub Actions)
- HTML output option for /fetch
- Health check enhancement — upstream connectivity checks on /health (deferred; not needed for single-instance self-host)

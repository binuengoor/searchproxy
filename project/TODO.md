# Project TODO & Progress

## Completed (Phase 1 — Scaffold & Architecture)
- [x] Create repo, initialize git, branch main
- [x] Create .gitignore
- [x] Write ARCHITECTURE.md (endpoints, env vars, fetch chain, API standards, best practices)
- [x] Write DECISIONS.md (design rationale)
- [x] Update CHANGELOG.md
- [x] Create project/ directory structure (TODO, DECISIONS, CHANGELOG_WORKING)
- [x] Define env variable schema and fetch chain architecture
- [x] Research and validate LiteLLM search router behavior (load balancing, fallback)
- [x] Research and validate Crawl4AI anti-bot capabilities
- [x] Research and validate free-tier anti-bot services (Scrape.do, ScraperAPI)
- [x] Research Jina AI services and rate limits
- [x] Rename `/search` → `/compat/perplexity`, `/research` → `/vane` (clear naming)
- [x] Add `SEARXNG_URL` env + image/video passthrough spec to `/compat/searxng`
- [x] Write docs/SearXNG_COMPAT.md (req/resp mapping, param matrix, error mapping)
- [x] Test Crawl4AI `/md` and `/crawl` endpoints on running instance
- [x] Determine Crawl4AI `/md` as primary fetch endpoint (not `/crawl`)
- [x] Scaffold `open-webui/` integration (skill.md, prompt.md, README.md)
- [x] Document OpenAPI auto-discovery as primary Open WebUI integration path
- [x] Update `.env.example` with `SEARCHPROXY_API_KEY`, `CRAWL4AI_LLM_*`, timeouts
- [x] Update `ARCHITECTURE.md` env block with `CRAWL4AI_LLM_*`, `SEARCHPROXY_API_KEY`

## Completed (Phase 2 — Build)
- [x] Create `pyproject.toml` with FastAPI, uvicorn, httpx, pydantic, python-dotenv
- [x] Create `app/config.py` — Pydantic Settings loading all env vars
- [x] Create `app/main.py` — FastAPI app with lifespan, API key middleware, `/health`
- [x] Implement `app/routers/search.py` — POST `/compat/perplexity` + alias `/v1/search`
- [x] Implement `app/services/litellm_search.py` — thin relay to LiteLLM
- [x] Implement `app/routers/searxng.py` — GET `/compat/searxng`
- [x] Implement `app/services/searxng_compat.py` — param mapping + image/video passthrough
- [x] Implement `app/routers/vane.py` — POST `/vane`
- [x] Implement `app/services/vane_proxy.py` — thin relay with streaming support
- [x] Implement `app/routers/fetch.py` — POST `/fetch`
- [x] Implement `app/services/crawl4ai.py` — `/md` primary, `/crawl` for extraction
- [x] Implement `app/services/jina_reader.py` — markdown fetch fallback
- [x] Implement `app/services/scrape_do.py` — anti-bot fallback #1
- [x] Implement `app/services/scraperapi.py` — anti-bot fallback #2
- [x] Implement `app/services/fetch_chain.py` — orchestration + anti-bot detection
- [x] Add credit tracking dict for Scrape.do + ScraperAPI (in-memory, resets on restart)
- [x] Write `Dockerfile` + `docker-compose.yml`
- [x] Basic tests: `tests/conftest.py` (async client fixtures for auth + unauth)
- [x] Git commit all code

## Recently Completed
- [x] Anti-bot body-scan for HTTP-200 responses from Crawl4AI and Jina Reader
- [x] `LOG_LEVEL` env var support in `app/main.py`
- [x] DNS override in `docker-compose.yml` (public DNS for anti-bot API reachability)
- [x] `SEARCHPROXY_REQUIRE_AUTH` + `LITELLM_API_KEY` added to `.env.example`
- [x] **Test suite: 27 passing tests (all endpoints):**
  - `tests/test_search_and_fetch.py` — 11 tests: search router + fetch chain service
  - `tests/test_searxng.py` — 6 tests: general search, images passthrough, videos passthrough, empty results, missing q (422), optional params
  - `tests/test_vane.py` — 6 tests: sync research, empty report, depth mapping, streaming chunks, streaming empty, default depth
  - `tests/test_fetch_http.py` — 4 tests: success, failed returns 200, format param, missing url (422)
- [x] Docker image built: `searchproxy:latest` at ~187MB
- [x] **Live test — all endpoints validated against real upstreams:**
  - `/health` ✅ (no auth)
  - `/compat/perplexity` ✅ (returns real search results from LiteLLM)
  - `/v1/search` alias ✅
  - `/compat/searxng` ✅ (general query + LiteLLM normalization)
  - `/compat/searxng?categories=images` ✅ (passthrough to SearXNG, returns image results)
  - `/vane` ✅ **FIXED during live test** — now calls correct `POST /api/search` endpoint with proper Vane JSON body (`chatModel`, `embeddingModel`, `optimizationMode`, `sources`, `history`, `stream`). Depth mapping: `concise→speed`, `balanced→balanced`, `comprehensive→quality`.
  - `/fetch` ✅ (Crawl4AI tier succeeds for most pages)
  - `/fetch` anti-bot ✅ (Cloudflare site escalated through Crawl4AI → Jina → ScraperAPI, returned 982KB markdown)
  - Auth middleware ✅ (`require_auth=true` blocks missing/wrong tokens on all routes; `/health`, `/docs`, `/openapi.json`, `/redoc` remain open)
- [x] **Git history scrubbed** with `git filter-repo` to remove `10.1.1.150` internal IP from all commits
- [x] `docker-compose.yml` comment fixed after filter-repo collateral
- [x] **End-to-end live test — ALL 22 checks pass:**
  - 1. `/health` = `{"status":"ok"}` ✅
  - 2. OpenAPI has all 5 endpoints ✅
  - 3. `/docs` = 200 ✅
  - 4. `/redoc` = 200 ✅
  - 5. `/compat/perplexity` returns 5 results ✅
  - 6. `/v1/search` (alias) returns 3 results ✅
  - 7. `/compat/searxng?q=python` returns 10 results ✅
  - 8. `/compat/searxng?categories=images` returns **370 results** ✅ (SearXNG passthrough working)
  - 9. `/compat/searxng?categories=videos` returns **92 results** ✅ (SearXNG passthrough working)
  - 10. `/vane` sync returns 3205-char report ✅
  - 11. `/vane?stream=true` returns 13496-char stream ✅
  - 12. `/fetch` example.com via Crawl4AI ✅
  - 13. `/fetch` nitter.net via Jina (anti-bot fallback) ✅
  - 14-17. Auth excluded paths (`/health`, `/docs`, `/openapi.json`, `/compat/perplexity` with `require_auth=false`) all return 200 ✅
  - 18-20. Missing params return 422 (`searxng` missing `q`, `fetch` missing `url`, `vane` missing `query`) ✅
  - 21. `/mcp` returns 404 (not implemented, expected) ✅
- [x] **SearXNG fixes applied:**
  - `app/main.py`: `httpx.AsyncClient(follow_redirects=True)` to handle upstream HTTP 308 redirects
  - `.env.example`: `SEARXNG_URL` now includes `/search` suffix to avoid redirect
  - `app/services/searxng_compat.py`: `field_validator` for `unresponsive_engines` handles SearXNG's list-of-lists format `[['brave.images','Suspended: too many requests'],...]` which caused Pydantic 500 errors
- [x] **`.env` kept untracked** — real API keys never committed to git history

## Known Issues / Limitations (from live test)
- [ ] `/vane` with long research queries can take 2+ minutes — Vane backend timeout, not searchproxy. Consider increasing `VANE_TIMEOUT` beyond 120s for `comprehensive` depth.
- [ ] `/vane` streaming endpoint (`?stream=true`) yields init handshake but chunk parsing may need client-side SSE handling (Vane returns server-sent events, not plain text chunks).
- [ ] `/compat/searxng` image/video passthrough: when SearXNG has no results, returns `count: 0` correctly, but client may want a clearer "no images found" message.
- [ ] `FETCH_TIMEOUT=30` is adequate for most pages, but anti-bot firebreak can add cumulative latency. Consider per-tier timeouts.
- [ ] Jina Reader API key is active and working; Scrape.do and ScraperAPI keys are also active (confirmed via anti-bot escalation test).

## In Progress
- [x] Validate `.env` file completeness and connectivity for all upstream services ✅

## Backlog (Phase 3 — Deploy)
- [ ] Deploy to ai-agents host (Docker) — blocked on GitHub auth
- [ ] Configure Open WebUI OpenAPI connection
- [ ] Test end-to-end: Open WebUI → searchproxy → LiteLLM → web search
- [ ] Test end-to-end: Open WebUI → searchproxy → Vane → deep research
- [ ] Test end-to-end: Open WebUI → searchproxy → Crawl4AI → fetch
- [ ] Push to GitHub (needs user PAT/SSH key auth setup)

## Backlog (Phase 4 — Enhance)
- [x] Serve Swagger UI at root (`GET /` → redirect to `/docs`) — **DONE** ✅
  - [x] Add `@app.get("/")` redirect in `app/main.py`
  - [x] Add `"/"` to `EXCLUDED_PATHS` for auth middleware
  - [x] Add `tests/test_main.py` with redirect + health tests (3/3 passing)
  - [x] Rebuild Docker image `searchproxy:latest` and restart container
  - [x] Live verified: `GET /` → 307 → `/docs` with `swagger-ui` in body
  - [x] Update `CHANGELOG.md`
- [x] **Router tests complete** (27 passing):
  - `tests/test_searxng.py` ✅ — `/compat/searxng` router (general + images + videos passthrough)
  - `tests/test_vane.py` ✅ — `/vane` sync + streaming with mocked `VaneProxyClient`
  - `tests/test_fetch_http.py` ✅ — `/fetch` HTTP endpoint (not just fetch_chain service)
- [ ] **Remaining tests to add**:
  - [ ] `tests/test_auth.py` — auth middleware (`require_auth=true`, invalid token, missing header, excluded paths)
  - [ ] `tests/test_openapi.py` — assert all 5 endpoints present in `/openapi.json`
  - [ ] Rename `tests/test_search_and_fetch.py` → `tests/test_search_and_fetch_chain.py` for clarity
- [ ] **MCP server layer** (`mcp_server.py` via stdio + SSE) — architecture-only, no implementation
- [ ] Add `/metrics` endpoint with Prometheus-style output
- [ ] Structured logging with JSON formatter + correlation_id
- [ ] Response caching (HTTP cache headers or Redis)

## Backlog (Future)
- [ ] Add Jina Reranker post-processing for search results
- [ ] CI/CD with GitHub Actions (lint, type-check, test)
- [ ] Open WebUI skill prompt A/B testing
- [ ] **Security**: `.env` contains real API keys (Jina, Scrape.do, ScraperAPI). File is git-ignored but was committed before ignore rule. Consider `git filter-repo` to scrub `.env` from history if repo goes public.

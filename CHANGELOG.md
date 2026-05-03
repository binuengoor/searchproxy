# Changelog

All notable changes to SearchProxy will be documented in this file.

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

### Architecture
- No custom search provider rotation — delegated entirely to LiteLLM router.
- No FlareSolverr — Crawl4AI's undetected browser + stealth modes replace brute-force headless.
- No manual provider lists or ranking logic — service is a thin gateway, not a router.
- Anti-bot services isolated: never invoked for routine fetches, only for confirmed anti-bot blocks.
- Jina Reader included with optional API key: enables higher rate limits (500 RPM vs 20 RPM) and future Jina services (Reranker, Embeddings).

## Planned

### 0.2.0
- MCP server layer (`mcp_server.py`) exposing `perplexity_search`, `fetch`, `vane_research` tools via stdio/SSE.
- Health check and metrics endpoints (`/health`, `/metrics`).
- Structured logging with correlation IDs across async requests.
- Add `max_results` client-side slicing after LiteLLM response normalization.
- Graceful degradation when Crawl4AI container is unreachable (skip to Jina instead of error).

### 0.3.0
- Jina Reranker integration for `/search` response post-processing.
- Request/response caching with TTL for identical search queries.
- Configurable fetch chain depth via query param.


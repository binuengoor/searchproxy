# SearchProxy

Self-hosted AI search & deep research gateway. High-speed 1-shot retrieval (`/v1/retrieve`), native 2-hop deep research (`/v1/research`), multi-tier anti-bot fetch chain (Crawl4AI → Jina Reader → Byparr Cloudflare solver → Anti-bot + Apache Tika), and compatibility bridges for OpenAI/Perplexity, Firecrawl, and SearXNG.

---

## What It Does

### MCP-Visible Tools (OpenAPI Spec — Auto-Discovered by Open WebUI / Agents)

| Tool | Endpoint | Purpose |
|------|----------|---------|
| **`retrieve`** | `POST /v1/retrieve` | **Fast 1-Shot Research (5–10s):** Multi-source LiteLLM search → BGE neural reranking → parallel fetch (Crawl4AI/Byparr/Tika) → structured citation synthesis with `[N]` inline citations. Supports SSE token streaming (`?stream=true`). |
| **`research`** | `POST /v1/research` | **Native 2-Hop Deep Research (15–25s):** Query decomposition into 2–3 focused sub-queries → parallel multi-search → candidate deduplication & domain diversity → BGE rerank → deep multi-source extraction → comprehensive cited research report. |
| **`fetch`** | `POST /fetch` | **Direct Document & Page Reader:** Multi-tier fetch pipeline (Crawl4AI → Jina Reader → Byparr Cloudflare solver → Scrape.do/ScraperAPI + Apache Tika for PDFs/docs). Returns clean, spam-stripped markdown. |
| **`health`** | `GET /health` | Liveness and readiness probe. |
| **`metrics`** | `GET /metrics` | Prometheus-style request, latency, and fetch tier metrics. |

### Runtime Compatibility Endpoints (Drop-In Protocols)

These endpoints provide seamless drop-in compatibility for external applications and frameworks without cluttering LLM tool discovery:

| Protocol / Client | Endpoint | Behavior |
|---|---|---|
| **OpenAI / Perplexity** | `POST /v1/chat/completions`<br>`POST /compat/perplexity/chat/completions` | Full OpenAI-compatible chat completion endpoint with SSE streaming and top-level `citations: [...]` array. |
| **Raw Search JSON** | `POST /v1/search`<br>`POST /compat/perplexity` | Raw search snippet JSON without fetching or LLM answer synthesis. |
| **Firecrawl v1 / v2** | `POST /compat/firecrawl/v1/scrape`<br>`POST /compat/firecrawl/v2/scrape` | Firecrawl v1/v2 scraper contract returning `{"success": true, "data": {"markdown": "...", "metadata": {...}}}`. |
| **SearXNG Search** | `GET /compat/searxng`<br>`GET /compat/searxng/search` | SearXNG-compatible JSON and HTML endpoint for Open WebUI web search RAG integration. |
| **Observability Logs** | `GET /logs`<br>`GET /api/logs` | Dark-themed live-refreshing request log viewer and JSON query API backed by SQLite. |

---

## Unified Docker Compose Suite

SearchProxy runs with all auxiliary scrapers, solvers, and parsers in a single cohesive Compose stack:

```yaml
services:
  searchproxy:
    image: ghcr.io/binuengoor/searchproxy:latest
    container_name: searchproxy
    ports:
      - "8080:8080"
    env_file:
      - .env
    environment:
      - CRAWL4AI_URL=http://crawl4ai:11235
      - BYPARR_URL=http://byparr:8191/v1
      - TIKA_URL=http://tika:9998/tika
      - SEARXNG_URL=http://searxng:8080/search
    extra_hosts:
      - "host.docker.internal:host-gateway"
    restart: unless-stopped
    depends_on:
      - crawl4ai
      - byparr
      - tika
      - searxng

  crawl4ai:
    image: unclecode/crawl4ai:latest
    container_name: searchproxy-crawl4ai
    ports:
      - "11235:11235"
    restart: unless-stopped
    shm_size: 1g

  byparr:
    image: ghcr.io/thephaseless/byparr:latest
    container_name: searchproxy-byparr
    ports:
      - "8191:8191"
    restart: unless-stopped

  tika:
    image: apache/tika:latest-full
    container_name: searchproxy-tika
    ports:
      - "9998:9998"
    restart: unless-stopped

  searxng:
    image: searxng/searxng:latest
    container_name: searchproxy-searxng
    ports:
      - "8980:8080"
    volumes:
      - ./searxng/config:/etc/searxng
      - ./searxng/data:/var/cache/searxng
    environment:
      - SEARXNG_BASE_URL=https://searxng.home.askbp.win
    restart: unless-stopped
```

---

## Quickstart

```bash
# 1. Clone & Configure
git clone https://github.com/binuengoor/searchproxy.git
cd searchproxy
cp .env.example .env

# 2. Launch the Stack
docker compose up -d

# 3. Test Liveness
curl http://localhost:8080/health

# 4. Execute 1-Shot Retrieval
curl -X POST http://localhost:8080/v1/retrieve \
  -H "Content-Type: application/json" \
  -d '{"query": "What are the latest developments in quantum computing?", "fetch_top_k": 5}'

# 5. Execute 2-Hop Deep Research
curl -X POST http://localhost:8080/v1/research \
  -H "Content-Type: application/json" \
  -d '{"query": "Compare Apple M4 Max vs Intel Core Ultra 9 285K for AI workloads and battery efficiency"}'
```

---

## Architecture & Pipelines

### 1. One-Shot Retrieval (`POST /v1/retrieve`)
```
User Query
  ├── LiteLLM Search (Tavily / Brave / SearXNG / Exa) ──► Raw Search Results
  ├── Deduplicate by Canonical URL Hash
  ├── Domain Filtering (include_domains / exclude_domains) & Diversity Capping
  ├── Neural BGE Reranker ──► Scored & Ranked Candidates
  ├── Parallel FetchChain (Crawl4AI → Jina → Byparr → Anti-bot + Tika for PDFs)
  ├── Spam/Boilerplate Content Stripper (20-60% token reduction)
  └── LLM Citation Synthesis ──► Sourced Response with [1], [2] Inline Citations
```

### 2. Native 2-Hop Deep Research (`POST /v1/research`)
```
User Deep Research Topic
  ├── Sub-Query Decomposition ──► Generates 2-3 focused sub-queries
  ├── Parallel Multi-Search ──► Dispatches parallel queries across all angles
  ├── Pool & Neural Rerank ──► Merges candidates & scores against root topic
  ├── Tiered Deep Extraction ──► Fetches top 6-8 diverse sources in parallel
  └── Comprehensive Report Synthesis ──► Executive Summary + Analysis + Takeaways
```

### 3. Tiered Fetch Chain (`POST /fetch`)
```
Target URL / Document
  ├── PDF / Binary Document? ──► Apache Tika (Direct text/metadata extraction)
  ├── 1. Crawl4AI (Local Container) ──► Fast headless browser rendering
  ├── 2. Jina Reader ──► High-fidelity markdown fallback
  ├── 3. Byparr Solver ──► Automated Cloudflare Turnstile & WAF challenge solver
  └── 4. Anti-Bot Firebreak (Paid APIs) ──► Scrape.do & ScraperAPI (quarantined)
```

---

## Configuration Reference

Key environment variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `SEARCHPROXY_REQUIRE_AUTH` | `false` | Enable Bearer token authentication |
| `SEARCHPROXY_API_KEY` | — | Secret API key for authentication |
| `LITELLM_SEARCH_URL` | `http://host.docker.internal:4000/search/unifiedsearch` | LiteLLM search router endpoint |
| `LITELLM_CHAT_URL` | `http://host.docker.internal:4000/v1/chat/completions` | LiteLLM chat completions endpoint |
| `LITELLM_CHAT_MODEL` | `openai/gpt-4o-mini` | Model used for citation synthesis |
| `CF_RERANK_URL` | `https://cf-inference.binuengoor.workers.dev/v1/rerank` | Cloudflare Workers BGE reranker endpoint |
| `CF_RERANK_MODEL` | `@cf/baai/bge-reranker-base` | Neural reranking model identifier |
| `CRAWL4AI_URL` | `http://crawl4ai:11235` | Internal Crawl4AI container endpoint |
| `BYPARR_URL` | `http://byparr:8191/v1` | Internal Byparr Cloudflare solver endpoint |
| `TIKA_URL` | `http://tika:9998/tika` | Internal Apache Tika parsing endpoint |
| `SEARXNG_URL` | `http://searxng:8080/search` | Internal SearXNG container endpoint |
| `MAX_PER_DOMAIN_SOURCES` | `2` | Maximum source documents from a single domain |
| `ENABLE_SSRF_PROTECTION` | `true` | Block private IP range fetches |

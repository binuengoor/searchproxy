# SearchProxy Architecture

Self-hosted AI search and deep research gateway. Built with FastAPI, SQLite observability, LiteLLM routing, BGE neural reranking, a 4-tier anti-bot fetch chain (with Byparr and Apache Tika), and a native Python 2-hop deep research engine.

---

## 1. System Architecture

```
                          ┌─────────────────────────────────────────────────────────┐
                          │            SearchProxy Suite (compose.yaml)             │
                          │                                                         │
                          │  ┌───────────────┐         ┌─────────────────────────┐  │
 [External Clients] ────► │  │  SearchProxy  │ ──────► │ Crawl4AI (:11235)        │  │
 (Open WebUI / Agents /   │  │  (:8080)      │         │ Primary JS & DOM Scraper│  │
  LiteLLM / Tools)        │  └───────┬───────┘         └─────────────────────────┘  │
                          │          │                                              │
                          │          ├───────────────► ┌─────────────────────────┐  │
                          │          │                 │ Byparr (:8191)          │  │
                          │          │                 │ Cloudflare Solver Tier  │  │
                          │          │                 └─────────────────────────┘  │
                          │          │                                              │
                          │          ├───────────────► ┌─────────────────────────┐  │
                          │          │                 │ Apache Tika (:9998)     │  │
                          │          │                 │ PDF & Binary Doc Parser │  │
                          │          │                 └─────────────────────────┘  │
                          │          │                                              │
                          │          └───────────────► ┌─────────────────────────┐  │
                          │                            │ SearXNG (:8980)         │  │
                          │                            │ Meta-Search Backend     │  │
                          │                            └─────────────────────────┘  │
                          └─────────────────────────────────────────────────────────┘
                                     │
                                     ▼
                ┌───────────────────────────────────────────────┐
                │             Upstream Infrastructure           │
                │                                               │
                │  • LiteLLM Router (:4000) (Search & Synthesis)│
                │  • Cloudflare Workers BGE Reranker            │
                │  • Jina Reader API                            │
                │  • Scrape.do / ScraperAPI (Quarantined)       │
                └───────────────────────────────────────────────┘
```

---

## 2. Core Service Capabilities

### A. One-Shot Retrieval (`POST /v1/retrieve`)
* **Latency:** 5–10s
* **Pipeline:**
  1. **Multi-Engine Search:** Dispatches queries to LiteLLM search router (Tavily, Brave, SearXNG, Exa).
  2. **Deduplication & Domain Filtering:** Normalizes canonical URLs, enforces whitelists/blacklists, and caps results per domain (`MAX_PER_DOMAIN_SOURCES`).
  3. **BGE Neural Reranking:** Scores candidates using `@cf/baai/bge-reranker-base` via Cloudflare Workers AI.
  4. **Speculative Prefetching:** Pre-fetches top candidate URLs in parallel while reranking runs.
  5. **Tiered Fetching:** Crawl4AI → Jina → Byparr → Paid Anti-bot + Tika for PDFs.
  6. **Boilerplate Stripping:** Spam and cookie banner cleaning via `ContentCleaner` (20–60% token reduction).
  7. **LLM Citation Synthesis:** Produces direct answer and key findings with inline `[N]` citations and streaming SSE support.

### B. Native 2-Hop Deep Research (`POST /v1/research`)
* **Latency:** 15–25s
* **Pipeline:**
  1. **Query Decomposition (Hop 1):** Fast LLM call breaks down complex topics into 2–3 distinct search sub-queries.
  2. **Parallel Sub-Search (Hop 2):** Dispatches concurrent searches across all angles and pools results.
  3. **Candidate Deduplication & Domain Diversity:** Merges candidates and filters out domain clusters.
  4. **Neural Reranking:** Scores the merged candidate pool against the original primary query.
  5. **Deep Multi-Source Extraction:** Reads 6–8 diverse sources in parallel via `FetchChain`.
  6. **Comprehensive Report Synthesis:** Generates a structured multi-section cited report (Executive Summary, Detailed Analysis, Key Takeaways).

### C. Multi-Tier Anti-Bot Fetch Chain (`POST /fetch`)
```
Target URL
  │
  ├── 0. PDF / Binary Document? ──► Apache Tika (Direct text extraction)
  │
  ├── 1. Crawl4AI (Local Container) ──► Fast headless browser rendering
  │      ├── Success ──► Clean Markdown
  │      └── 5xx/Timeout? ──► Transient retry once after 1s
  │
  ├── 2. Jina Reader ──► High-fidelity markdown fallback
  │      ├── Success ──► Clean Markdown
  │      └── 403 / Cloudflare Challenge detected? ──► Escalates to Byparr
  │
  ├── 3. Byparr Cloudflare Solver ──► Automated local Turnstile & WAF challenge solver
  │      └── Success ──► Clean Markdown
  │
  └── 4. Anti-Bot Firebreak (Paid APIs) ──► Scrape.do → ScraperAPI (Quarantined credits)
```

---

## 3. Compatibility Bridges

1. **OpenAI / Perplexity Chat (`POST /v1/chat/completions`, `POST /compat/perplexity/chat/completions`):**
   Full drop-in OpenAI-compatible chat endpoint with SSE streaming and top-level `citations: [...]` array.
2. **Firecrawl v1 / v2 (`POST /compat/firecrawl/v1/scrape`, `POST /compat/firecrawl/v2/scrape`):**
   Standard Firecrawl response envelope (`{"success": true, "data": {...}}`) powered by SearchProxy's 4-tier fetch engine.
3. **SearXNG Search (`GET /compat/searxng`, `GET /compat/searxng/search`):**
   SearXNG JSON and HTML search endpoint for Open WebUI web search integration.

---

## 4. Observability & Performance

* **SQLite Observability (`/logs`, `/api/logs`):** Embedded SQLite database recording every request, response status, latency, fetch tier, and tokens with 7-day automatic retention.
* **Prometheus Metrics (`/metrics`):** Standard Prometheus counters and histograms for request latency, fetch tier breakdown, and error rates.
* **SQLite Result Caching (`/data/cache.db`):** Optional SHA256-keyed cache with configurable TTLs for search, fetch, rerank, and synthesis.

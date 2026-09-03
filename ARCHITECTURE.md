# SearchProxy Architecture

Self-hosted AI search and deep research gateway. Built with FastAPI, SQLite caching and observability, native multi-provider search rotation (Tavily, Brave, Exa, Serper, SearXNG), local in-memory ONNX neural reranking, a 5-tier fetch chain (FastFetch → Crawl4AI → Jina → Byparr → Anti-bot + Apache Tika), and a native Python 2-hop deep research engine.

---

## 1. System Architecture

```
                          ┌─────────────────────────────────────────────────────────┐
                          │            SearchProxy Suite (compose.yaml)             │
                          │                                                         │
                          │  ┌───────────────┐         ┌─────────────────────────┐  │
 [External Clients] ────► │  │  SearchProxy  │ ──────► │ Crawl4AI (:11235)        │  │
 (Open WebUI / Pi Agent / │  │  (:8080)      │         │ Headless Chromium       │  │
  SDKs / Agents / Tools)  │  │               │         └─────────────────────────┘  │
                          │  │  • Search     │                                      │
                          │  │    Router     │ ──────► ┌─────────────────────────┐  │
                          │  │  • FastFetch  │         │ Byparr (:8191)          │  │
                          │  │  • Local ONNX │         │ Cloudflare Solver Tier  │  │
                          │  │    Reranker   │         └─────────────────────────┘  │
                          │  └───────┬───────┘                                      │
                          │          │                 ┌─────────────────────────┐  │
                          │          ├───────────────► │ Apache Tika (:9998)     │  │
                          │          │                 │ PDF & Binary Doc Parser │  │
                          │          │                 └─────────────────────────┘  │
                          │          │                                              │
                          │          └───────────────► ┌─────────────────────────┐  │
                          │                            │ SearXNG (:8980)         │  │
                          │                            │ Safety Net Search Tier  │  │
                          │                            └─────────────────────────┘  │
                          └─────────────────────────────────────────────────────────┘
                                     │
                                     ▼
                ┌───────────────────────────────────────────────┐
                │             Upstream Infrastructure           │
                │                                               │
                │  • Direct Search APIs (Tavily, Brave, Exa,    │
                │    Serper) — Quota Rotated with 429 Failover  │
                │  • OpenAI-Compatible LLM Gateway (Groq /      │
                │    Cerebras / Cloudflare / OpenRouter)        │
                │  • Cloudflare Workers BGE Reranker (Fallback) │
                │  • Jina Reader API (Markdown Fallback)        │
                │  • Scrape.do / ScraperAPI (Quarantined)       │
                └───────────────────────────────────────────────┘
```

---

## 2. Core Service Capabilities

### A. One-Shot Retrieval (`POST /v1/retrieve`)
* **Latency:** 2–5s
* **Pipeline:**
  1. **Native Multi-Provider Search:** Dispatches queries via `SearchRouter` rotating round-robin across active free quotas (Tavily, Brave, Exa, Serper) with automatic 429 failover to local SearXNG (Tier 2).
  2. **Deduplication & Domain Filtering:** Normalizes canonical URLs, enforces whitelists/blacklists, and caps results per domain (`MAX_PER_DOMAIN_SOURCES`).
  3. **Local ONNX Neural Reranking:** Scores candidates in ~15–25ms using quantized `BAAI/bge-reranker-base` via `fastembed` with hardware AVX-512 VNNI acceleration (with Cloudflare Workers AI fallback).
  4. **Speculative Prefetching:** Pre-fetches top candidate URLs in parallel while reranking executes.
  5. **Tiered Fetching:** FastFetch (HTTP + Trafilatura) → Crawl4AI → Jina → Byparr → Paid Anti-bot + Tika for PDFs.
  6. **Boilerplate Stripping:** Spam and cookie banner cleaning via `ContentCleaner` (20–60% token reduction).
  7. **OpenAI-Compatible LLM Synthesis:** Calls low-latency LPU endpoints (Groq, Cerebras, CF, OpenRouter) to produce structured responses with inline `[N]` citations and streaming SSE support.

### B. Native 2-Hop Deep Research (`POST /v1/research`)
* **Latency:** 15–25s
* **Pipeline:**
  1. **Query Decomposition (Hop 1):** Fast LLM call breaks down complex topics into 2–3 distinct search sub-queries.
  2. **Parallel Sub-Search (Hop 2):** Dispatches concurrent searches across all angles using rotated search providers.
  3. **Candidate Deduplication & Domain Diversity:** Merges candidates and filters out domain clusters.
  4. **Local Neural Reranking:** Scores the merged candidate pool against the original primary query.
  5. **Deep Multi-Source Extraction:** Reads 6–8 diverse sources in parallel via `FetchChain`.
  6. **Comprehensive Report Synthesis:** Generates a structured multi-section cited report (Executive Summary, Detailed Analysis, Key Takeaways).

### C. Multi-Tier Fetch Chain (`POST /fetch`)
```
Target URL
  │
  ├── 0. PDF / Binary Document? ──► Apache Tika (Direct text extraction)
  │
  ├── 1. FastFetch (HTTP + Trafilatura) ──► Ultra-fast static HTML extraction (~40ms)
  │      └── Anti-bot / SPA (<300 chars)? ──► Falls through to headless crawler
  │
  ├── 2. Crawl4AI (Local Container) ──► Fast headless browser rendering for SPAs
  │      ├── Success ──► Clean Markdown
  │      └── 5xx/Timeout? ──► Transient retry once after 1s
  │
  ├── 3. Jina Reader ──► High-fidelity markdown fallback
  │      ├── Success ──► Clean Markdown
  │      └── 403 / Cloudflare Challenge detected? ──► Escalates to Byparr
  │
  ├── 4. Byparr Cloudflare Solver ──► Automated local Turnstile & WAF challenge solver
  │      └── Success ──► Clean Markdown
  │
  └── 5. Anti-Bot Firebreak (Paid APIs) ──► Scrape.do → ScraperAPI (Quarantined credits)
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

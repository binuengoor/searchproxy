# SearchProxy Code Review: Maintainability, Efficiency, Latency

**Scope:** End-to-end review of the v0.8.2 pipeline with focus on `/v1/retrieve`.
**Commit reviewed:** `6cd4b0b` on `ai-agents:~/git/searchproxy` (current HEAD).
**Baseline observed:** Uncached retrieve with `fetch_top_k=2` takes ~15–25s.

---

## 1. Executive Summary

**Verdict:** The architecture is solid. The pipeline is well-decomposed, properly async, and degrades gracefully. The biggest wins are **not** structural refactor — they are **tunable parameters** and **one concurrency pattern fix**. Refactor only `retrieve_service.py` into smaller steps if it grows past 800 lines.

**Top 3 latency wins (trivial to implement):**
1. Reduce synthesis prompt budget from 20k → 12k chars (~2k tokens saved, ~3–5s faster TTFT)
2. Cap speculative prefetch at 3 URLs instead of all `fetch_top_k` (less wasted work)
3. Fix the `asyncio.wait()` + nested cancellation race in the fetch batch (reliability)

---

## 2. Architecture Assessment

### 2.1 What's Strong

| Area | Observation |
|------|------------|
| **Module structure** | Flat `app/routers/` + `app/services/`. Routers are <30 lines. Good. |
| **Config** | Single source of truth in `app/config.py`. No `os.environ` scattered. Good. |
| **Client lifecycle** | One `httpx.AsyncClient` via `lifespan`, shared everywhere. `http2=True`. Good. |
| **Error handling** | Graceful degradation at every step (search → rerank → fetch → synthesis). Good. |
| **Fetch chain** | Tiered fallback (Crawl4AI → Jina → anti-bot) with `skip_firebreak` control. Good. |
| **Prefetch overlap** | Rerank and top-K fetch happen concurrently. Saves 1–2s. Good. |
| **Correlation IDs** | All logs carry `x-correlation-id`. Makes debugging traceable. Good. |

### 2.2 What's Brittle

| Area | Risk |
|------|------|
| **`retrieve_service.py` scope creep** | 604 lines with 6+ pipeline steps + quality gates + budget logic. One class, many hats. |
| **`asyncio.wait()` fetch pattern** | Manual `done`/`pending` unwrapping with nested `asyncio.timeout()` per URL. Cancellation race risk. |
| **No structured latency metrics** | Log lines say "fetched X in Ys" but no P50/P95 histograms per step. Flying blind on outliers. |
| **Hardcoded per-URL timeout** | `per_url_timeout = 30.0` — when batch timeout is 15s, a single slow URL starves the batch silently. |

---

## 3. Latency Deep Dive

### 3.1 Pipeline Breakdown

```
Step 1: Search     → 1–3s     (LiteLLM/Perplexity, cached)
Step 2: Dedup      → <1ms     (in-memory dict)
Step 3: Rerank     → 0.5–2s   (BGE via cf-inference)
        └── Prefetch (overlap) → starts here
Step 4: Fetch      → 2–8s     (parallel, up to fetch_top_k URLs)
Step 5: Synthesis  → 2–10s    (LiteLLM chat / GPT-4o-mini)
─────────────────────────────────────────
Total uncached     → ~15–25s
Total cached       → ~1–2s
```

### 3.2 Hotspot 1: Synthesis Prompt Size

- `RETRIEVE_MAX_TOTAL_CONTENT = 20000` chars
- At ~4 chars/token ≈ **5k prompt tokens**
- GPT-4o-mini is fast but 5k tokens still adds 2–4s to TTFT vs 3k tokens
- Budget step already distributes by relevance weight — content is already truncated per source
- **Fix:** Drop to 12000 chars. Quality loss is marginal; speed gain is real.

### 3.2 Hotspot 2: Speculative Prefetch Waste

Current:
```python
prefetch_count = min(fetch_top_k, len(deduped))
```

If `fetch_top_k=5`, all 5 URLs are prefetched. But rerank may reorder such that URL #5 drops out. That prefetch effort is cancelled (wasted bandwidth + downstream load).

- **Fix:** Cap speculative prefetch at 3. Most queries only need top-3 sources for a good answer. Remaining URLs are fetched fresh after rerank confirms their rank.
- **Expected savings:** 1–2 wasted fetches per query when fetch_top_k ≥ 4.

### 3.3 Hotspot 3: Fetch Timeout Race

Current pattern in `_fetch_step`:
```python
async def _fetch_one(...) -> FetchResult:
    async with asyncio.timeout(per_url_timeout):  # inner: 30s
        return await self._fetch.execute(...)

done, pending = await asyncio.wait(
    fetch_tasks,
    timeout=fetch_timeout,  # outer: 15s
    return_when=asyncio.ALL_COMPLETED
)
for task in pending:
    task.cancel()
```

**Problem:** `asyncio.wait()` with timeout fires at the batch level. Tasks in `pending` are cancelled, but if they're inside the inner `asyncio.timeout(30)` block, the cancellation may race with the timeout context manager. In some edge cases the task never actually stops and hangs the event loop. This is a known footgun with nested `asyncio.wait()` + `asyncio.timeout()`.

**Fix:** Replace with:
```python
results = await asyncio.gather(
    *[asyncio.wait_for(t, timeout=per_url_timeout) for t in fetch_tasks],
    return_exceptions=True,
)
# Then unwrap
```
`asyncio.gather` + `asyncio.wait_for` per-task handles cancellation cleanly without the `done`/`pending` manual bookkeeping.

### 3.4 Hotspot 4: Per-URL Timeout is Static

- `per_url_timeout = 30.0` (hardcoded)
- When batch timeout is 15s and you fetch 5 URLs, a single URL can consume the ENTIRE 15s and still not trigger its individual timeout. The remaining 4 URLs never get a fair shot.

**Fix:** Dynamic per-URL timeout:
```python
per_url_timeout = max(4.0, min(10.0, self._settings.RETRIEVE_FETCH_TIMEOUT / fetch_top_k + 2))
```

Examples:
- fetch_top_k=2, batch=15s: per_url = max(4, min(10, 15/2+2)) = **9.5s**
- fetch_top_k=5, batch=15s: per_url = max(4, min(10, 15/5+2)) = **5.0s**
- fetch_top_k=10, batch=15s: per_url = max(4, min(10, 15/10+2)) = **3.5s** → clamped to **4.0s**

This ensures no single URL starves the batch.

### 3.5 Hotspot 5: No Rerank Cache

`CacheService` caches search results and fetch results, but NOT rerank scores. If the same query comes in twice within minutes, you still pay for reranking.

- **Fix:** Cache rerank results keyed by `(query_hash, hash_of_doc_list)` with a short TTL (e.g., 5 minutes).
- Only worth it if you see repeat query volume. Low ROI otherwise.

---

## 4. Code Quality Issues

### 4.1 `retrieve_service.py` (604 lines)

Each step (`_search_step`, `_dedup_step`, `_rerank_step`, `_fetch_step`, `_budget_step`) is well-isolated, but the file still carries too many concerns. Binu's preference is low-maintenance — I would **not** split this into 5 files. Instead:

- Keep the class
- Make `_fetch_one` a proper instance method (not a closure with default params for loop capture)
- Extract `_is_likely_paywall`, `_is_too_short`, `_canonical_key`, `_truncate_content` into `app/services/retrieve_utils.py`
- Target: get `retrieve_service.py` under 400 lines

### 4.2 `content_cleaner.py` (402 lines of regex)

- Uses trafilatura + aggressive regex passes
- Works but is fragile — every new site layout can break the regex assumptions
- Not a priority to change, but monitor for accuracy regression

### 4.3 `dependencies.py` — Double-Checked Locking

Singleton factories use `threading.Lock()` + double-check. This is correct for Python async (only one event loop per process for uvicorn workers). Not an issue.

### 4.4 Tests

- `tests/test_retrieve.py` hangs in Docker pytest (confirmed during testing)
- Root cause: likely `pytest-asyncio` + `AsyncMock` + task interaction
- **Fix:** Update to `pytest-asyncio>=0.21` pattern with `asyncio_mode = "auto"` in `pyproject.toml`

---

## 5. Efficiency Observations

### 5.1 Fetch Chain Short-Circuits Correctly

```python
for tier in ["crawl4ai", "jina"] + anti_bot:
    result = await self._execute_single_tier(tier, ...)
    if result.success:
        break
```

Good: Crawl4AI success means Jina and anti-bot are skipped. No wasted downstream calls.

### 5.2 BM25 Content Filtering at Source

Crawl4AI is called with `f=bm25&q=<query>` which reduces content by 60–80% before it even reaches the synthesis prompt. This is a smart optimization and should be kept.

### 5.3 httpx Connection Pool

```python
limits=httpx.Limits(max_keepalive_connections=20, max_connections=100)
```

Peak concurrent connections: search(1) + rerank(1) + fetch(≤5) + synthesis(1) = ~8. Current limits are fine. If `fetch_top_k` goes to 10, bump `max_connections` to 200.

### 5.4 SQLite Cache

- `CacheService` uses SQLite with WAL mode (confirmed: `journal_mode=wal`)
- WAL mode allows reads to proceed during writes. Good for concurrent fetch caching.
- TTLs: search=5min, fetch=1day. Reasonable.

---

## 6. Priority-Ordered Recommendations

### 🔴 High (implement now)

| # | Change | Effort | Expected Impact |
|---|--------|--------|-----------------|
| 1 | **Reduce `RETRIEVE_MAX_TOTAL_CONTENT` to 12000** | 5 min | 3–5s faster synthesis |
| 2 | **Cap speculative prefetch at 3 URLs** | 15 min | Less wasted downstream load |
| 3 | **Replace `asyncio.wait()` with `asyncio.gather(return_exceptions=True)`** | 1 hr | Eliminates timeout race hangs |
| 4 | **Add per-step latency histograms** (P50/P95) | 2 hrs | Enables data-driven optimization |

### 🟡 Medium (next sprint)

| # | Change | Effort |
|---|--------|--------|
| 5 | **Dynamic per-URL timeout** based on `fetch_top_k` | 30 min |
| 6 | **Extract retrieve utilities** to reduce file size | 1 hr |
| 7 | **Fix `test_retrieve.py` pytest-asyncio hang** | 30 min |
| 8 | **Add rerank result caching** (if repeat query volume warrants it) | 1 hr |

### 🟢 Low (nice to have)

| # | Change | Effort |
|---|--------|--------|
| 9 | **Bump `max_connections`** if `fetch_top_k` exceeds 8 | 5 min |
| 10 | **ML-based spam detection** as replacement for regex quality gates | High |

---

## 7. Specific Code Snippet: Recommended `_fetch_step` Refactor

Current (lines ~240–277 in `6cd4b0b`):

```python
# PROBLEMATIC: asyncio.wait() + manual unwrapping + nested timeout
done, pending = await asyncio.wait(fetch_tasks, timeout=fetch_timeout, return_when=asyncio.ALL_COMPLETED)
for task in pending:
    task.cancel()
fetch_results: list[Any] = []
for task in fetch_tasks:
    if task in done:
        try:
            fetch_results.append(task.result())
        except Exception as exc:
            fetch_results.append(exc)
    else:
        fetch_results.append(asyncio.TimeoutError(f"Fetch timed out after {fetch_timeout}s"))
```

Recommended:

```python
# CLEAN: asyncio.gather with per-task wait_for
per_url_timeout = max(4.0, min(10.0, fetch_timeout / len(top_urls) + 2))

async def _fetch_one(url: str) -> FetchResult | Exception:
    try:
        return await asyncio.wait_for(
            self._fetch.execute(
                url,
                aggressive_clean=True,
                skip_firebreak=False,
                content_filter=content_filter,
                content_query=content_query,
            ),
            timeout=per_url_timeout,
        )
    except asyncio.TimeoutError:
        return asyncio.TimeoutError(f"Fetch timed out after {per_url_timeout}s")
    except Exception as exc:
        return exc

results: list[FetchResult | Exception] = await asyncio.gather(
    *[_fetch_one(url_info["url"]) for url_info in top_urls],
    return_exceptions=False,  # we handle exceptions inside _fetch_one
)
```

This pattern:
- Eliminates the `done`/`pending` manual state machine
- Gives each URL a fair timeout slice
- Handles exceptions inside the coroutine so `gather` doesn't abort
- Cancels prefetch discards cleanly (no race conditions)

---

## 8. Bottom Line

Don't refactor the architecture — it's clean. Focus on:
1. **One tunable** (`RETRIEVE_MAX_TOTAL_CONTENT`)
2. **One cap** (prefetch limit)
3. **One pattern fix** (`asyncio.gather`)
4. **One metric** (latency histograms)

These four changes together should drop uncached retrieve from ~22s to ~14–16s without any infrastructure changes.

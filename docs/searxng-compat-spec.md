# SearXNG Compatibility Spec

This document defines how `/compat/searxng` maps SearXNG requests/responses to/from LiteLLM search.

## Request Mapping

### SearXNG Parameters → LiteLLM Parameters

| SearXNG Param | Type | Required | Mapped To | Notes |
|---------------|------|----------|-----------|-------|
| `q` | string | **Yes** | `query` | 1:1 pass-through |
| `format` | string | **Yes** | — | Must be `"json"`. Other formats (`csv`, `rss`) return `400`. |
| `categories` | string | No | — | **Ignored.** LiteLLM router decides categories based on query semantics. |
| `engines` | string | No | — | **Ignored.** LiteLLM router handles engine selection internally. |
| `language` | string | No | — | **Ignored.** LiteLLM providers handle locale internally. Could map to `country` in future. |
| `pageno` | int | No | — | **Ignored.** LiteLLM does not support pagination. Always returns page 1. |
| `time_range` | string | No | — | **Ignored.** Could be mapped to provider-specific params in future. |
| `safesearch` | int | No | — | **Ignored.** LiteLLM does not expose this. |
| `autocomplete` | string | No | — | **Ignored.** |
| `results_on_new_tab` | int | No | — | **Ignored.** |
| `image_proxy` | bool | No | — | **Ignored.** |
| `enabled_plugins` | list | No | — | **Ignored.** |
| `disabled_plugins` | list | No | — | **Ignored.** |
| `enabled_engines` | list | No | — | **Ignored.** |
| `disabled_engines` | list | No | — | **Ignored.** |

### Parameters We Inject (Not from SearXNG client)

| Parameter | Value | Notes |
|-----------|-------|-------|
| `max_results` | `min(client_param, 20)` | SearXNG clients may pass `count` or `num`. We cap at 20. Default: 10. |

### Parameter SearXNG Supports But LiteLLM Doesn't

LiteLLM cannot currently handle:
- Category filtering (`categories=science`)
- Engine selection (`engines=duckduckgo`)
- Pagination (`pageno=2`)
- Time range (`time_range=month`)
- Safe search toggle (`safesearch=1`)
- Language filtering (`language=en`)

**Decision:** We silently ignore these. A future version can map `time_range` and `safesearch` to LiteLLM's provider-specific parameters if the router gains support.

---

## Response Mapping

### LiteLLM Response Format

```json
{
  "object": "search",
  "results": [
    {
      "title": "...",
      "url": "https://...",
      "snippet": "...",
      "date": "2024-01-15"
    }
  ]
}
```

### SearXNG Response Format (Target)

```json
{
  "query": "searched query",
  "number_of_results": 7,
  "results": [
    {
      "title": "Result Title",
      "url": "https://example.com",
      "content": "Snippet text...",
      "engine": "duckduckgo",
      "score": 1.0,
      "category": "general"
    }
  ],
  "answers": [],
  "corrections": [],
  "suggestions": [],
  "infoboxes": [],
  "unresponsive_engines": []
}
```

### Field-by-Field Mapping

| SearXNG Field | Source | Mapping |
|---------------|--------|---------|
| `query` | SearXNG request `q` | Echo back |
| `number_of_results` | `len(litellm.results)` | Count of actual results returned |
| `results[]` | `litellm.results` | 1:1 mapping with field renames |
| `results[].title` | `result.title` | Direct pass |
| `results[].url` | `result.url` | Direct pass |
| `results[].content` | `result.snippet` | Rename: `snippet` → `content` |
| `results[].engine` | — | Static `"litellm"` (LiteLLM is a meta-aggregator, not a single engine) |
| `results[].score` | — | Static `1.0` (no ranking score available from LiteLLM) |
| `results[].category` | — | Static `"general"` (LiteLLM does not expose categories) |
| `answers[]` | — | Empty array (LiteLLM has no direct answer feature) |
| `corrections[]` | — | Empty array |
| `suggestions[]` | — | Empty array |
| `infoboxes[]` | — | Empty array |
| `unresponsive_engines[]` | — | Empty array |

### Design Decision: Why Empty Arrays for Extras

SearXNG consumers (n8n, LangChain, custom scripts) typically check for `result.get("answers")` or `len(data["suggestions"])`. Returning empty arrays keeps them from crashing with `KeyError` while honestly signaling that these features are not available through the LiteLLM relay.

---

## Unsupported SearXNG Features

| Feature | Status | Rationale |
|---------|--------|-----------|
| HTML output | ❌ Not supported | This is a JSON API gateway only |
| CSV format | ❌ Not supported | Return `400` |
| RSS format | ❌ Not supported | Return `400` |
| Pagination (`pageno > 1`) | ⚠️ Ignored | LiteLLM has no pagination |
| Image search (`categories=images`) | ✅ Passthrough | Forwarded to upstream SearXNG if `SEARXNG_URL` is configured. Otherwise, returns empty `results[]` (graceful degradation). |
| Video search (`categories=videos`) | ✅ Passthrough | Same as images. Detected from `categories` or `engines` params. |

---

## Error Mapping

| Our Error | SearXNG Equivalent | HTTP Status |
|-----------|---------------------|-------------|
| LiteLLM router down / timeout | `unresponsive_engines: ["litellm"]` | `503` |
| Missing `q` parameter | SearXNG returns empty results | `400` |
| Invalid `format` | SearXNG returns `403` | `400` (we're more lenient) |
| No results found | `number_of_results: 0`, empty `results[]` | `200` |

---

## Future Enhancements

1. **Reranking:** If Jina Reranker is enabled, we could inject `score` from reranker output instead of static `1.0`.
2. **DeepSearch:** Jina DeepSearch could populate `answers[]` with synthesized answers.
3. **Category Detection:** Parse LiteLLM `search_domain_filter` or detect category from query to populate `results[].category`.
4. **Time Range Mapping:** When LiteLLM supports `time_range`, map SearXNG's `time_range` to it.

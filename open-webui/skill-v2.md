# SearchProxy Skill V2 — Agentic Research via /v1/retrieve

Use this when setting up Open WebUI with SearchProxy for deep research. This skill replaces Vane for quality queries.

## Goal

Enable an LLM to perform multi-step, iterative web research using only `/v1/retrieve`, eliminating Vane's timeout risk while producing higher-quality sourced answers.

## Prerequisites

- SearchProxy deployed and running (Docker or native).
- Open WebUI with Functions enabled.
- An OpenAPI tool server registered in Open WebUI pointing to `http://searchproxy:8080/openapi.json` (or wherever SearchProxy lives).

## Setup Steps

### 1. Register the OpenAPI Tool Server

In Open WebUI: **Admin Panel → Functions → OpenAPI (Function) Server**

- Name: `searchproxy`
- URL: `http://searchproxy:8080/openapi.json`
- Authentication: leave blank if `SEARCHPROXY_REQUIRE_AUTH=false` (home network)

### 2. Enable Function Calling

Set the model's **Function Calling** to **Native**.

Without this, the model will not make tool calls.

### 3. Set the System Prompt

Paste the contents of `prompt-v2.md` into the model's system prompt field.

Key instruction: Do NOT call `/vane`. Use iterative `/v1/retrieve` calls instead.

### 4. Verify Tools Appear

In a chat, you should see the model able to call:
- `/v1/retrieve` — primary search+synthesize
- `/compat/perplexity` — fast snippets
- `/fetch` — URL extraction
- `/compat/searxng` — raw search or media
- `/vane` — exists but the prompt tells the model to avoid it

### 5. Test a Deep Query

Example: "Compare React and Vue adoption in 2025 with performance benchmarks"

Expected behavior:
1. Model decomposes into sub-questions.
2. Calls `/v1/retrieve` for each sub-question (2–4 calls).
3. Cross-checks and identifies gaps.
4. May call one follow-up `/v1/retrieve`.
5. Synthesizes a unified answer with unified citations and a Sources section.

## Architecture Difference from V1

| | V1 (prompt.md) | V2 (prompt-v2.md) |
|---|---|---|
| Recommended tool | `/vane` | `/v1/retrieve` × N |
| Timeout risk | High on quality mode | None |
| Decomposition | Backend-side (Vane) | Model-side (prompt) |
| Max iterations | Hardcoded in Vane (up to 25) | Model stops when confident |
| Conversation aware | Vane's internal session | Open WebUI's native history |
| When to use | Speed/balanced queries | Any analysis requiring synthesis |

## Why This Works Better Than Vane Quality Mode

1. **No timeout wall:** Each `/v1/retrieve` call is 5–15s. The model controls pacing.
2. **Stronger fetch chain:** SearchProxy uses Crawl4AI + Jina + anti-bot fallbacks. Vane uses Playwright + Readability.
3. **BGE reranking:** Sources are relevance-scored before fetch. Vane gets raw SearxNG.
4. **Model context:** Sub-question answers accumulate in the chat, letting the model spot contradictions.
5. **Cost control:** Caller decides how many retrieve calls to make. No black-box 25-round burn.

## Migration from V1

If currently using `skill.md` + `prompt.md`:
1. Replace the model's system prompt with `prompt-v2.md`.
2. Update the model's skill reference from `skill.md` to `skill-v2.md`.
3. Remove any Open WebUI workflows that explicitly call `/vane`.
4. Ask the same queries. The model will now use iterative `/v1/retrieve` instead.

No backend changes needed.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Model still calls `/vane` | Prompt not applied to the active model preset | Check model settings in Open WebUI; paste prompt-v2.md directly |
| Model makes 10+ retrieve calls | Model following "decompose" too aggressively | Add per-chat instruction: "Use no more than 3 retrieve calls for this question" |
| Citations don't match sources | Model using local citation numbers from each retrieve call | Instruct model to build a unified source list or check the citation handling section of prompt-v2.md |
| Slow even with /v1/retrieve | Fetch chain hitting anti-bot / retries | Normal on difficult sites; check SearchProxy logs for fetch_tier and retry counts |

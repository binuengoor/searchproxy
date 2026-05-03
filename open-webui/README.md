# SearchProxy Open WebUI Setup

Connect searchproxy to Open WebUI as a Perplexity-style research assistant with live web search, deep research, and URL fetch.

## Prerequisites

- searchproxy is running and accessible from the Open WebUI host
- You know the searchproxy host, port, and API key (if auth is enabled)
- Your model supports Native Mode (function calling / tool use)

## Step 1: Connect the OpenAPI Server

1. **Admin Panel → Settings → Tools**
2. Click **+** to add a new connection → **OpenAPI (Function) Server**
3. Fill in:
   - **URL:** `http://<searchproxy-host>:8080/openapi.json`
   - **Auth:** Bearer token (`SEARCHPROXY_API_KEY`) — or skip if `SEARCHPROXY_REQUIRE_AUTH=false`
4. Save

Open WebUI auto-discovers all searchproxy endpoints as callable tools.

## Step 2: Configure the Model

1. **Workspace → Models** → Edit your model
2. **Capabilities** → ensure **Function Calling = Native** (Agentic Mode). Legacy/default mode does not support tool calling.
3. **System Prompt** → paste the **Prompt Text** section from `prompt.md`
4. Under **Skills**, attach `searchproxy-research-assistant` (created in Step 3)

## Step 3: Import the Skill

1. **Workspace → Skills** → **+** → name it `searchproxy-research-assistant`
2. Paste the contents of `skill.md`
3. Save

## Step 4: Verify

Start a new chat with your configured model. Try these in order:

**Quick search** — should trigger a search tool:
> "Who won the Champions League final in 2025?"

**Deep research** — should trigger the Vane/research tool:
> "Compare Qwen 3 32B vs Llama 4 70B for local self-hosted deployment"

**URL fetch** — should trigger the fetch tool:
> "Summarize this page: https://example.com"

**Media search** — should trigger SearXNG with categories:
> "Find images of the 2025 Wimbledon trophy"

## How It Works

**OpenAPI auto-discovery**: Open WebUI fetches `/openapi.json` from searchproxy and generates tool schemas automatically. The model sees endpoint names, parameters, and response schemas.

**Prompt + Skill split**: The `prompt.md` system prompt defines *identity and decision rules* — when to answer directly, when to search, when to go deep. The `skill.md` defines *endpoint-specific behavior* — parameter choices, anti-patterns, architecture context. This separation keeps the system prompt compact (important: Open WebUI duplicates it on every tool call in Agentic Mode, so shorter = fewer wasted tokens).

**Why no tool names in the prompt**: The model receives tool definitions from the OpenAPI schema via the function-calling API. Repeating tool names in the prompt wastes tokens and breaks when endpoints change. The prompt describes *intent* (search / deep research / fetch / media); the model maps that to whatever tools are available.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No tool calls appear | Ensure Function Calling = Native for the model. Legacy mode doesn't support tools. |
| "Connection refused" | Check `SEARCHPROXY_API_KEY` matches. Verify searchproxy host:port is reachable from Open WebUI. |
| Empty search results | Check `LITELLM_SEARCH_URL` is configured and LiteLLM router is healthy. |
| `/vane` returns error | Check `VANE_URL` is configured and Vane service is running. |
| Model uses wrong tool | Re-paste the latest `prompt.md` — old prompts reference old tool names. |
| Model over-calls tools | The prompt explicitly guards against this, but some models need the decision rules reinforced. Check that Native Mode is enabled (not prompt-injection mode). |

## Files

| File | Purpose |
|------|---------|
| `prompt.md` | System prompt — paste into model's system prompt field |
| `skill.md` | Behavioral skill — endpoint semantics, parameters, anti-patterns |
| `README.md` | This setup guide |
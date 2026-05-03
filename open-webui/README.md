# SearchProxy Open WebUI Setup

Connect searchproxy to Open WebUI via the OpenAPI (Function) Server so all endpoints are auto-discovered as tools.

## Prerequisites

- searchproxy is running and accessible from the Open WebUI host
- You know the searchproxy host, port, and API key

## Step 1: Connect the OpenAPI Server

1. Open WebUI → **Admin Panel** → **Settings** → **Connections**
2. Click **+** to add a new connection
3. Select **OpenAPI (Function) Server**
4. Fill in:
   - **URL:** `http://<searchproxy-host>:8080/openapi.json` (adjust port if needed)
   - **Auth Type:** Bearer Token
   - **Token:** Your `SEARCHPROXY_API_KEY`
5. Click **Save**

Open WebUI will auto-discover all endpoints (`/compat/perplexity`, `/vane`, `/fetch`) as callable tools.

## Step 2: Import the Skill

1. Open WebUI → **Workspace** → **Skills**
2. Click **+** to create a new skill
3. Name it: `searchproxy-research-assistant`
4. Paste the contents of `skill.md` into the skill editor
5. Save

## Step 3: Attach the Skill to Your Model

1. Open WebUI → **Workspace** → **Models**
2. Edit the model you want to use with searchproxy
3. Under **Skills**, attach `searchproxy-research-assistant`
4. Under **Capabilities**, ensure **Native Function Calling** is enabled
5. Paste the contents of `prompt.md` into the **System Prompt** field
6. Save the model

## Step 4: Verify in Chat

Start a new chat with your configured model. Ask:

> "Who won the Champions League final in 2025?"

You should see a tool call badge `search_proxy` appear briefly, followed by the answer.

Then try:

> "Compare Qwen 3 32B vs Llama 4 70B for local self-hosted deployment"

This should trigger `vane_research` (or whatever the auto-discovered name is for `/vane`).

## How It Works

Open WebUI fetches the `/openapi.json` spec from searchproxy and generates tool schemas automatically. The model sees:

- Endpoint names
- HTTP methods (GET, POST)
- Parameter schemas (types, required/optional)
- Response schemas

The `skill.md` file provides the *behavioral rules* — telling the model which endpoint to use for which type of query. The `prompt.md` establishes the identity and execution style.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No tool calls appear | Ensure Native Function Calling is enabled for the model. Default/legacy mode does not support automatic tool calling. |
| "Connection refused" | Check `SEARCHPROXY_API_KEY` matches. Verify searchproxy host:port is reachable from Open WebUI container. |
| Empty search results | Check that `LITELLM_SEARCH_URL` is configured and the LiteLLM router is healthy. |
| `/vane` returns error | Check `VANE_URL` is configured and the Vane service is running. |

## Files in This Folder

| File | Purpose |
|------|---------|
| `skill.md` | Behavioral guidance — when to use which endpoint |
| `prompt.md` | System prompt copy-paste for model configuration |
| `README.md` | This setup guide |

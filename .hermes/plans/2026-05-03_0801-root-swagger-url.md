# Plan: Root Swagger URL (`GET /`)

## Goal
Serve Swagger UI at the root path (`/`) of the searchproxy service. This provides:
1. **Immediate API discoverability** — anyone hitting the service URL sees all endpoints, schemas, and can test live.
2. **Zero-config health/status check** — if the Swagger page loads, the service is up and its OpenAPI spec is being generated correctly.

## Current State
- FastAPI app configured with `docs_url="/docs"`, `redoc_url="/redoc"`.
- `GET /` currently returns **404** (no route registered).
- `GET /docs` and `GET /redoc` both work and return 200.
- Auth middleware `EXCLUDED_PATHS` = `{"/health", "/openapi.json", "/docs", "/redoc"}` — `/` is NOT excluded, so if `SEARCHPROXY_REQUIRE_AUTH=true`, the root Swagger would be blocked.

## Proposed Approach

### Step 1 — Add root redirect endpoint
Register `@app.get("/", include_in_schema=False)` in `app/main.py` that returns a `RedirectResponse` to `/docs`. FastAPI handles the redirect; no custom HTML needed.

Why redirect vs. inline Swagger HTML:
- **Redirect** reuses FastAPI's existing `/docs` handler — no duplication of Swagger-UI assets or version drift.
- **Redirect** keeps a single source of truth for the interactive docs.
- Downside: URL bar shows `/docs`, not `/`. Acceptable tradeoff.

### Step 2 — Exclude `/` from auth middleware
Add `"/"` to `EXCLUDED_PATHS` in `app/main.py` so the root Swagger works even when `SEARCHPROXY_REQUIRE_AUTH=true`. Rationale: the root path is purely diagnostic/discovery — it contains no data and performs no state mutation. All actual API calls still require auth.

### Step 3 — Add test
Write a test in `tests/test_main.py` (new file) or add to an existing test file:
- Assert `GET /` returns 307/308 redirect to `/docs`
- Assert `GET /docs` (follow redirect) returns 200 and contains `swagger-ui` in response body
- Assert `GET /` without auth still works when `require_auth=true`

If creating `tests/test_main.py`, also move the existing `/health` test from `tests/test_search_and_fetch.py` into it for logical grouping.

## Files to Change
1. `app/main.py` — add `@app.get("/")` redirect; update `EXCLUDED_PATHS`
2. `tests/test_main.py` — new file with root redirect + health tests
3. `project/TODO.md` — mark as completed
4. `CHANGELOG.md` — add entry

## Risks & Tradeoffs
- **URL exposure**: Root path now reveals the service is a FastAPI app and lists all endpoints. Mitigation: this is identical information already exposed by `/docs` and `/openapi.json`, which are already open. No new attack surface.
- **Path collision**: None. `/` was previously 404.
- **Redirect loop**: Impossible — redirect target is `/docs`, not `/`.

## Validation Steps
1. `curl -I http://localhost:8080/` → `HTTP/1.1 307 Temporary Redirect` with `location: /docs`
2. `curl http://localhost:8080/` → follows redirect, returns Swagger UI HTML
3. `pytest tests/test_main.py` passes
4. Auth test: set `SEARCHPROXY_REQUIRE_AUTH=true`, `curl http://localhost:8080/` still returns 200/redirect

---
name: searchproxy-project
description: >-
  Manage, document, and track project roadmap, architecture decisions, code reviews, and tactical progress for SearchProxy using the local project/ working directory. Use when planning features, reviewing project status, logging architectural decisions, or tracking tasks.
---

# SearchProxy Project Management & Documentation Skill

This skill outlines the procedures for tracking progress, managing tasks, logging architectural decisions, and maintaining project documentation in SearchProxy.

---

## 1. Project Working Directory (`project/`)

The [`project/`](file:///Users/millionmax/Documents/Git/searchproxy/project) folder is the dedicated working directory for local planning, active work tracking, and architectural records. It is excluded from Git (`.gitignore`) to keep development planning artifacts decoupled from production source code.

### Directory Structure & Responsibilities

- **[`project/TODO.md`](file:///Users/millionmax/Documents/Git/searchproxy/project/TODO.md)**:
  - Active sprint backlog, stage checklists, and tactical debt.
  - Mark completed tasks with `[x]` as soon as verified.
  - Group new tasks logically under stage/version headings.
- **[`project/ROADMAP.md`](file:///Users/millionmax/Documents/Git/searchproxy/project/ROADMAP.md)**:
  - High-level strategic vision, stage breakdowns (e.g., Stage 1 to Stage 4), and future priorities.
  - Documents explicitly skipped features with rationale.
- **[`project/DECISIONS.md`](file:///Users/millionmax/Documents/Git/searchproxy/project/DECISIONS.md)**:
  - Architectural Decision Records (ADRs).
  - Format: Context, Decision, Consequences / Trade-offs, and Alternatives considered.
- **[`project/CODE_REVIEW.md`](file:///Users/millionmax/Documents/Git/searchproxy/project/CODE_REVIEW.md)**:
  - Audit logs, code smell observations, performance bottlenecks, and security reviews.
- **[`project/NOTES.md`](file:///Users/millionmax/Documents/Git/searchproxy/project/NOTES.md)**:
  - Technical scratchpad, debugging logs, operational notes, and environment tips.

---

## 2. Core Project Architecture Reference

SearchProxy is a high-performance research and search aggregation gateway built on FastAPI.

### Key Endpoints
1. **`POST /v1/retrieve`**:
   - Pipeline: Search (LiteLLM/SearXNG) → Deduplicate → Rerank (BGE) → Parallel Multi-tier Fetch → Content Quality Gates → LLM Synthesis with `[N]` inline citations.
   - Supports SSE streaming (`?stream=true`).
2. **`POST /vane`**:
   - Deep research proxy forwarding to Vane / Perplexica backend.
3. **`POST /fetch`**:
   - Resilient multi-tier URL fetching: Crawl4AI → Jina → Anti-bot firebreak.
4. **`GET /health` & `GET /metrics`**:
   - Liveness and Prometheus metrics endpoints.

---

## 3. Workflow Runbook for Planning & Tracking

When working on SearchProxy tasks:

1. **Check Status**:
   - Read [`project/TODO.md`](file:///Users/millionmax/Documents/Git/searchproxy/project/TODO.md) and [`project/ROADMAP.md`](file:///Users/millionmax/Documents/Git/searchproxy/project/ROADMAP.md) before starting any new phase or feature.
2. **Document Decisions**:
   - If an architectural trade-off or dependency change is made, append a new ADR entry to [`project/DECISIONS.md`](file:///Users/millionmax/Documents/Git/searchproxy/project/DECISIONS.md).
3. **Verify Changes**:
   - Run tests: `pytest tests/` (or specific test suites) to ensure 0 regressions.
4. **Update Trackers**:
   - Update [`project/TODO.md`](file:///Users/millionmax/Documents/Git/searchproxy/project/TODO.md) with completed checkmarks and notes.
   - Keep [`CHANGELOG.md`](file:///Users/millionmax/Documents/Git/searchproxy/CHANGELOG.md) updated for public versioned releases.

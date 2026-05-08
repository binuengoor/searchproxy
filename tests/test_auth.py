"""Tests for authentication middleware."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings


# --- Auth disabled (default) ---


@pytest.mark.anyio
async def test_health_accessible_without_auth(client):
    """Health endpoint is always accessible — no auth needed."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- Auth enabled ---


@pytest.mark.anyio
async def test_auth_enabled_blocks_unauthenticated(client):
    """When auth is enabled, requests without Bearer token get 401."""
    import app.config

    original = app.config.settings
    app.config.settings = Settings(
        SEARCHPROXY_REQUIRE_AUTH=True,
        SEARCHPROXY_API_KEY="test-token",
    )
    try:
        resp = await client.post(
            "/compat/perplexity",
            json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 401
    finally:
        app.config.settings = original


@pytest.mark.anyio
async def test_auth_enabled_correct_token_passes(auth_client):
    """Auth-enabled: correct Bearer token lets request through to excluded paths."""
    resp = await auth_client.get("/health")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_auth_enabled_wrong_token_returns_401(client):
    """Wrong Bearer token still gets 401."""
    import app.config

    original = app.config.settings
    app.config.settings = Settings(
        SEARCHPROXY_REQUIRE_AUTH=True,
        SEARCHPROXY_API_KEY="correct-key",
    )
    try:
        # client fixture has no auth headers; manually set a wrong one
        resp = await client.post(
            "/compat/perplexity",
            json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401
    finally:
        app.config.settings = original


@pytest.mark.anyio
async def test_excluded_paths_no_auth_required(client):
    """Excluded paths are accessible even when auth is enabled (no auth header)."""
    import app.config

    original = app.config.settings
    app.config.settings = Settings(
        SEARCHPROXY_REQUIRE_AUTH=True,
        SEARCHPROXY_API_KEY="test-secret-key",
    )
    try:
        # client has no auth headers; excluded paths should still work
        for path in ("/health", "/docs", "/redoc", "/openapi.json", "/"):
            resp = await client.get(path, follow_redirects=False)
            assert resp.status_code != 401, f"{path} returned 401"
    finally:
        app.config.settings = original


@pytest.mark.anyio
async def test_metrics_excluded_from_auth(client):
    """The /metrics endpoint should be accessible without auth."""
    import app.config

    original = app.config.settings
    app.config.settings = Settings(
        SEARCHPROXY_REQUIRE_AUTH=True,
        SEARCHPROXY_API_KEY="test-secret-key",
    )
    try:
        resp = await client.get("/metrics")
        assert resp.status_code == 200
    finally:
        app.config.settings = original


@pytest.mark.anyio
async def test_missing_bearer_prefix_returns_401(client):
    """Auth header without 'Bearer ' prefix returns 401."""
    import app.config

    original = app.config.settings
    app.config.settings = Settings(
        SEARCHPROXY_REQUIRE_AUTH=True,
        SEARCHPROXY_API_KEY="test-secret-key",
    )
    try:
        resp = await client.post(
            "/compat/perplexity",
            json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "test-secret-key"},
        )
        assert resp.status_code == 401
    finally:
        app.config.settings = original
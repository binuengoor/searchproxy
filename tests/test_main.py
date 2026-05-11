from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.main import app as fastapi_app


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_root_redirects_to_docs(client: AsyncClient):
    """GET / returns 307 redirect to /docs for quick browser testing."""
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/docs"


@pytest.mark.anyio
async def test_root_redirect_with_auth_enabled(monkeypatch, anyio_backend):
    """Root path is excluded from auth — works even when require_auth=true."""
    import httpx
    import app.clients
    import app.config

    _real_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    monkeypatch.setattr(app.clients, "_client", _real_client)

    original_settings = app.config.settings
    app.config.settings = app.config.Settings(
        SEARCHPROXY_REQUIRE_AUTH=True,
        SEARCHPROXY_API_KEY="live-token",
    )

    from httpx import ASGITransport
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
    ) as ac:
        resp = await ac.get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/docs"

    app.config.settings = original_settings
    await _real_client.aclose()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_health_no_auth(client: AsyncClient):
    """/health is always accessible without auth."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

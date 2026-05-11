from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.main import app as fastapi_app


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_dependencies(monkeypatch):
    """Reset DI singletons between tests so mutations don't leak."""
    import app.dependencies as deps
    deps._cache_service = None
    deps._fetch_chain = None
    deps._litellm_client = None
    deps._rerank_service = None
    deps._synthesis_service = None
    deps._retrieve_service = None
    deps._searxng_service = None
    deps._vane_client = None


@pytest.fixture
async def client(monkeypatch, anyio_backend):
    """httpx AsyncClient against the FastAPI app.

    Patches ``app.clients._client`` so that ``get_client()`` works and
    Lifespan DI helpers never see an uninitialized state.
    """
    import httpx
    import app.clients

    # Real httpx client — enough for the DI layer, but routes mocked later
    _real_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    monkeypatch.setattr(app.clients, "_client", _real_client)

    from httpx import ASGITransport
    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as ac:
        yield ac

    await _real_client.aclose()


@pytest.fixture
async def auth_client(monkeypatch, anyio_backend):
    """Authenticated test client (auth enabled)."""
    import httpx
    import app.clients
    import app.config

    _real_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    monkeypatch.setattr(app.clients, "_client", _real_client)

    original_settings = app.config.settings
    app.config.settings = app.config.Settings(
        SEARCHPROXY_REQUIRE_AUTH=True,
        SEARCHPROXY_API_KEY="test-token",
    )

    from httpx import ASGITransport
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as ac:
        yield ac

    app.config.settings = original_settings
    await _real_client.aclose()

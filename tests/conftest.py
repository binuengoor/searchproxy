from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Unauthenticated test client (for /health checks)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client() -> AsyncClient:
    """AsyncClient pointing at the ASGI app, no auth headers."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Authenticated test client (bearer token pre-set)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def auth_client() -> AsyncClient:
    """AsyncClient with a valid SEARCHPROXY_API_KEY Authorization header."""
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {settings.SEARCHPROXY_API_KEY}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        yield ac

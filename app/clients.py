"""Shared HTTP client lifecycle management.

Module-level _client is initialized in main.py lifespan and consumed by
DI factories in dependencies.py. Keeping it in a dedicated module avoids
circular imports between main.py and dependencies.py.
"""
from __future__ import annotations

import httpx

# Module-level shared client, initialized in lifespan
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Return the shared httpx client. Raises if called before startup."""
    if _client is None:
        raise RuntimeError("httpx client not initialized")
    return _client

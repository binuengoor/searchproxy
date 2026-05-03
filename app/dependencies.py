"""FastAPI dependency helpers.

Centralizes all DI factory functions so routers stay thin and service
construction is consistent. Each router imports only the `Depends()` it needs.
"""

from __future__ import annotations

from app.config import settings
from app.main import get_client
from app.services.fetch_chain import FetchChain
from app.services.litellm_search import LiteLLMSearchClient
from app.services.searxng_compat import SearxngCompatService
from app.services.vane_proxy import VaneProxyClient


def get_fetch_chain() -> FetchChain:
    """Build a FetchChain from shared infrastructure."""
    return FetchChain(client=get_client(), settings=settings)


def get_litellm_client() -> LiteLLMSearchClient:
    """Build a LiteLLMSearchClient from shared infrastructure."""
    return LiteLLMSearchClient(client=get_client(), settings=settings)


def get_searxng_service() -> SearxngCompatService:
    """Build a SearxngCompatService from shared infrastructure."""
    return SearxngCompatService(
        litellm_client=LiteLLMSearchClient(client=get_client(), settings=settings),
        http_client=get_client(),
        settings=settings,
    )


def get_vane_client() -> VaneProxyClient:
    """Build a VaneProxyClient from shared infrastructure."""
    return VaneProxyClient(client=get_client(), settings=settings)
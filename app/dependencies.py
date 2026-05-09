"""FastAPI dependency helpers.

Centralizes all DI factory functions so routers stay thin and service
construction is consistent. Each router imports only the `Depends()` it needs.
"""

from __future__ import annotations

from app.config import settings
from app.main import get_client
from app.services.cache import CacheService
from app.services.fetch_chain import FetchChain
from app.services.litellm_search import LiteLLMSearchClient
from app.services.rerank_service import RerankService
from app.services.retrieve_service import RetrieveService
from app.services.searxng_compat import SearxngCompatService
from app.services.synthesis_service import SynthesisService
from app.services.vane_proxy import VaneProxyClient

_cache_service: "CacheService | None" = None


def _get_cache() -> CacheService:
    """Return the shared CacheService singleton (lazy init)."""
    global _cache_service
    if _cache_service is None:
        _cache_service = CacheService(settings=settings)
    return _cache_service


def get_fetch_chain() -> FetchChain:
    """Build a FetchChain from shared infrastructure."""
    return FetchChain(client=get_client(), settings=settings, cache=_get_cache())


def get_litellm_client() -> LiteLLMSearchClient:
    """Build a LiteLLMSearchClient from shared infrastructure."""
    return LiteLLMSearchClient(client=get_client(), settings=settings, cache=_get_cache())


def get_rerank_service() -> RerankService:
    """Build a RerankService from shared infrastructure."""
    return RerankService(client=get_client(), settings=settings)


def get_synthesis_service() -> SynthesisService:
    """Build a SynthesisService from shared infrastructure."""
    return SynthesisService(client=get_client(), settings=settings)


def get_retrieve_service() -> RetrieveService:
    """Build a RetrieveService (full pipeline) from shared infrastructure."""
    client = get_client()
    cache = _get_cache()
    return RetrieveService(
        search_client=LiteLLMSearchClient(client=client, settings=settings, cache=cache),
        fetch_chain=FetchChain(client=client, settings=settings, cache=cache),
        rerank_service=RerankService(client=client, settings=settings),
        synthesis_service=SynthesisService(client=client, settings=settings),
        settings=settings,
    )


def get_searxng_service() -> SearxngCompatService:
    """Build a SearxngCompatService from shared infrastructure."""
    return SearxngCompatService(
        litellm_client=LiteLLMSearchClient(client=get_client(), settings=settings, cache=_get_cache()),
        http_client=get_client(),
        settings=settings,
    )


def get_vane_client() -> VaneProxyClient:
    """Build a VaneProxyClient from shared infrastructure."""
    return VaneProxyClient(client=get_client(), settings=settings)
"""FastAPI dependency helpers.

Centralizes all DI factory functions so routers stay thin and service
construction is consistent. Services are singletons — created once and reused
across requests — since they hold no per-request state beyond a shared
httpx.AsyncClient and settings reference.
"""

from __future__ import annotations

import threading

from app.clients import get_client
from app.config import settings
from app.services.cache import CacheService
from app.services.fetch_chain import FetchChain
from app.services.litellm_search import LiteLLMSearchClient
from app.services.rerank_service import RerankService
from app.services.retrieve_service import RetrieveService
from app.services.searxng_compat import SearxngCompatService
from app.services.synthesis_service import SynthesisService
from app.services.vane_proxy import VaneProxyClient

_lock = threading.RLock()
_cache_service: CacheService | None = None
_fetch_chain: FetchChain | None = None
_litellm_client: LiteLLMSearchClient | None = None
_rerank_service: RerankService | None = None
_synthesis_service: SynthesisService | None = None
_retrieve_service: RetrieveService | None = None
_searxng_service: SearxngCompatService | None = None
_vane_client: VaneProxyClient | None = None


def _get_cache() -> CacheService:
    """Return the shared CacheService singleton (lazy init, thread-safe)."""
    global _cache_service
    if _cache_service is None:
        with _lock:
            if _cache_service is None:
                _cache_service = CacheService(settings=settings)
    return _cache_service


def get_fetch_chain() -> FetchChain:
    """Return the shared FetchChain singleton (thread-safe lazy init)."""
    global _fetch_chain
    if _fetch_chain is None:
        with _lock:
            if _fetch_chain is None:
                _fetch_chain = FetchChain(client=get_client(), settings=settings, cache=_get_cache())
    return _fetch_chain


def get_litellm_client() -> LiteLLMSearchClient:
    """Return the shared LiteLLMSearchClient singleton (thread-safe lazy init)."""
    global _litellm_client
    if _litellm_client is None:
        with _lock:
            if _litellm_client is None:
                _litellm_client = LiteLLMSearchClient(client=get_client(), settings=settings, cache=_get_cache())
    return _litellm_client


def get_rerank_service() -> RerankService:
    """Return the shared RerankService singleton (thread-safe lazy init)."""
    global _rerank_service
    if _rerank_service is None:
        with _lock:
            if _rerank_service is None:
                _rerank_service = RerankService(client=get_client(), settings=settings, cache=_get_cache())
    return _rerank_service


def get_synthesis_service() -> SynthesisService:
    """Return the shared SynthesisService singleton (thread-safe lazy init)."""
    global _synthesis_service
    if _synthesis_service is None:
        with _lock:
            if _synthesis_service is None:
                _synthesis_service = SynthesisService(client=get_client(), settings=settings)
    return _synthesis_service


def get_retrieve_service() -> RetrieveService:
    """Return the shared RetrieveService (full pipeline) singleton (thread-safe lazy init)."""
    global _retrieve_service
    if _retrieve_service is None:
        with _lock:
            if _retrieve_service is None:
                _retrieve_service = RetrieveService(
                    search_client=get_litellm_client(),
                    fetch_chain=get_fetch_chain(),
                    rerank_service=get_rerank_service(),
                    synthesis_service=get_synthesis_service(),
                    settings=settings,
                    cache=_get_cache(),
                )
    return _retrieve_service


def get_searxng_service() -> SearxngCompatService:
    """Return the shared SearxngCompatService singleton (thread-safe lazy init)."""
    global _searxng_service
    if _searxng_service is None:
        with _lock:
            if _searxng_service is None:
                _searxng_service = SearxngCompatService(
                    litellm_client=get_litellm_client(),
                    http_client=get_client(),
                    settings=settings,
                )
    return _searxng_service


def get_vane_client() -> VaneProxyClient:
    """Return the shared VaneProxyClient singleton (thread-safe lazy init)."""
    global _vane_client
    if _vane_client is None:
        with _lock:
            if _vane_client is None:
                _vane_client = VaneProxyClient(client=get_client(), settings=settings)
    return _vane_client

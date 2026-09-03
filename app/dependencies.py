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
from app.services.search import SearchRouter
from app.services.rerank_service import RerankService
from app.services.retrieve_service import RetrieveService
from app.services.deep_research_service import DeepResearchService
from app.services.searxng_compat import SearxngCompatService
from app.services.synthesis_service import SynthesisService

_lock = threading.RLock()
_cache_service: CacheService | None = None
_fetch_chain: FetchChain | None = None
_search_router: SearchRouter | None = None
_rerank_service: RerankService | None = None
_synthesis_service: SynthesisService | None = None
_retrieve_service: RetrieveService | None = None
_searxng_service: SearxngCompatService | None = None
_deep_research_service: DeepResearchService | None = None


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


def get_search_router() -> SearchRouter:
    """Return the shared SearchRouter singleton (thread-safe lazy init)."""
    global _search_router
    if _search_router is None:
        with _lock:
            if _search_router is None:
                _search_router = SearchRouter(client=get_client(), settings=settings, cache=_get_cache())
    return _search_router


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
                    search_client=get_search_router(),
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
                    search_client=get_search_router(),
                    http_client=get_client(),
                    settings=settings,
                )
    return _searxng_service


def get_deep_research_service() -> DeepResearchService:
    """Return the shared DeepResearchService singleton (thread-safe lazy init)."""
    global _deep_research_service
    if _deep_research_service is None:
        with _lock:
            if _deep_research_service is None:
                _deep_research_service = DeepResearchService(
                    search_client=get_search_router(),
                    rerank_service=get_rerank_service(),
                    fetch_chain=get_fetch_chain(),
                    synthesis_service=get_synthesis_service(),
                    settings=settings,
                    http_client=get_client(),
                )
    return _deep_research_service


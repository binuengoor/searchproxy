from __future__ import annotations

import logging
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """All configuration loaded from environment variables.

    Only this module reads env vars. No os.environ.get() elsewhere.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Core ---
    SEARCHPROXY_REQUIRE_AUTH: bool = Field(default=False)
    SEARCHPROXY_API_KEY: str = "change-me-in-production"

    # --- Compat: Perplexity / OpenAI ---
    LITELLM_SEARCH_URL: str = Field(default="http://litellm-host:4000/search/unifiedsearch")
    LITELLM_API_KEY: str | None = Field(default=None)

    # --- Retrieve: LiteLLM Chat (synthesis) ---
    LITELLM_CHAT_URL: str = Field(
        default="http://host.docker.internal:4000/v1/chat/completions",
        description="LiteLLM chat completions endpoint for /v1/retrieve synthesis.",
    )
    LITELLM_CHAT_MODEL: str = Field(
        default="openai/gpt-4o-mini",
        description="LiteLLM model name for synthesis. Must match a model available on your LiteLLM proxy.",
    )

    # --- Retrieve: BGE Reranker (cf-inference) ---
    CF_RERANK_URL: str = Field(
        default="https://cf-inference.binuengoor.workers.dev/v1/rerank",
        description="BGE reranker endpoint (cf-inference /v1/rerank).",
    )
    CF_RERANK_API_KEY: str | None = Field(
        default=None,
        description="API key for cf-inference reranker. Omit if the endpoint is open.",
    )
    CF_RERANK_MODEL: str = Field(
        default="@cf/baai/bge-reranker-base",
        description="Model identifier for cf-inference reranker.",
    )
    RERANK_TIMEOUT: float = Field(
        default=10.0,
        description="Timeout in seconds for the BGE reranker call.",
    )
    CF_RERANK_API_KEY: str | None = Field(
        default=None,
        description="API key for cf-inference reranker. Omit if the endpoint is open.",
    )
    CF_RERANK_MODEL: str = Field(
        default="@cf/baai/bge-reranker-base",
        description="Model identifier for cf-inference reranker.",
    )

    # --- Retrieve: tuning ---
    RETRIEVE_MAX_CONTENT_PER_SOURCE: int = Field(
        default=6000,
        description="Max characters of fetched content per source for synthesis.",
    )
    RETRIEVE_MAX_TOTAL_CONTENT: int = Field(
        default=12000,
        description="Max total characters across all sources for synthesis prompt.",
    )
    RETRIEVE_RERANK_TOP_K: int = Field(
        default=20,
        description="Number of search results to send to the reranker.",
    )
    RETRIEVE_MIN_CONTENT_LENGTH: int = Field(
        default=300,
        description="Minimum characters of fetched content for a source to be included in synthesis.",
    )
    RETRIEVE_FETCH_TIMEOUT: float = Field(
        default=15.0,
        description="Timeout in seconds for the parallel fetch phase of /v1/retrieve.",
    )
    RETRIEVE_PREFETCH_DURING_RERANK: bool = Field(
        default=True,
        description="Speculatively start fetching top search results during rerank to overlap latency.",
    )
    RETRIEVE_PREFETCH_MAX: int = Field(
        default=3,
        description="Max number of URLs to speculatively prefetch during rerank. Caps wasted fetches when fetch_top_k > 3.",
    )
    SYNTHESIS_MAX_TOKENS: int = Field(
        default=2048,
        description="Max tokens for the LLM synthesis response.",
    )
    SYNTHESIS_TIMEOUT: float = Field(
        default=60.0,
        description="Timeout in seconds for LLM synthesis calls.",
    )

    # --- Caching ---
    CACHE_ENABLED: bool = Field(default=False, description="Enable SQLite caching for search, fetch, and rerank results.")
    CACHE_SEARCH_TTL: int = Field(default=300, description="TTL for search cache entries in seconds.")
    CACHE_FETCH_TTL: int = Field(default=86400, description="TTL for fetch cache entries in seconds.")
    CACHE_RERANK_TTL: int = Field(default=300, description="TTL for rerank cache entries in seconds.")
    CACHE_DB_PATH: str = Field(default="/data/cache.db", description="Path to SQLite cache database.")

    # --- Vane deep research ---
    VANE_URL: str = Field(default="http://vane-host:3001")
    VANE_CHAT_PROVIDER_ID: str | None = Field(default=None)
    VANE_CHAT_MODEL_KEY: str | None = Field(default=None)
    VANE_EMBED_PROVIDER_ID: str | None = Field(default=None)
    VANE_EMBED_MODEL_KEY: str | None = Field(default=None)

    # --- Compat: SearXNG passthrough ---
    SEARXNG_URL: str | None = Field(default=None)

    # --- Fetch: Crawl4AI ---
    CRAWL4AI_URL: str = Field(default="http://crawl4ai-host:11235")
    CRAWL4AI_LLM_PROVIDER: str | None = Field(default=None)
    CRAWL4AI_LLM_BASE_URL: str | None = Field(default=None)
    CRAWL4AI_LLM_API_KEY: str | None = Field(default=None)

    # --- Fetch: Jina Reader ---
    JINA_API_KEY: str | None = Field(default=None)

    # --- Fetch: Anti-bot (quarantined) ---
    SCRAPE_DO_API_KEY: str | None = Field(default=None)
    SCRAPERAPI_API_KEY: str | None = Field(default=None)

    # --- Logging ---
    LOG_LEVEL: str = Field(default="INFO")
    LOG_FORMAT: str = Field(default="text", description="Log format: text or json")

    # --- Observability ---
    OBSERVABILITY_ENABLED: bool = Field(default=False)
    OBSERVABILITY_DB_PATH: str = Field(default="/data/observability.db")
    OBSERVABILITY_RETENTION_DAYS: int = Field(default=7)

    # --- Timeouts ---
    FETCH_TIMEOUT: int = Field(default=30)  # generic fallback; per-tier overrides below
    SEARCH_TIMEOUT: int = Field(default=15)
    VANE_TIMEOUT: int = Field(default=120)
    CRAWL4AI_TIMEOUT: int = Field(default=15)
    JINA_TIMEOUT: int = Field(default=15)
    ANTIBOT_TIMEOUT: int = Field(default=45)

    # --- Connect timeout (shared across all HTTP clients) ---
    CONNECT_TIMEOUT: float = Field(
        default=5.0,
        description="Connect timeout in seconds for all downstream HTTP clients.",
    )

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        if self.SEARCHPROXY_REQUIRE_AUTH and self.SEARCHPROXY_API_KEY == "change-me-in-production":
            logger.warning(
                "SEARCHPROXY_API_KEY is still set to the default value. "
                "Set a secure key in production."
            )


settings = Settings()

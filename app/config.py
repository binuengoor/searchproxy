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

    # --- (rest of settings unchanged) ---

    # --- Compat: Perplexity / OpenAI ---
    LITELLM_SEARCH_URL: str = Field(default="http://litellm-host:4000/search/unifiedsearch")
    LITELLM_API_KEY: str | None = Field(default=None)

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

    # --- Timeouts ---
    FETCH_TIMEOUT: int = Field(default=30)
    SEARCH_TIMEOUT: int = Field(default=15)
    VANE_TIMEOUT: int = Field(default=120)

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        if self.SEARCHPROXY_REQUIRE_AUTH and self.SEARCHPROXY_API_KEY == "change-me-in-production":
            logger.warning(
                "SEARCHPROXY_API_KEY is still set to the default value. "
                "Set a secure key in production."
            )


settings = Settings()

"""Scrape.do service client — anti-bot firebreak."""

from __future__ import annotations

from app.services.anti_bot_base import AntiBotClient


class ScrapeDoClient(AntiBotClient):
    """Standalone async client for the Scrape.do anti-bot service.

    Inherits credit tracking, error handling, and graceful degradation
    from :class:`AntiBotClient`. Only service-specific details are defined here.
    """

    _SERVICE_NAME = "scrape_do"
    _API_URL_TEMPLATE = "https://api.scrape.do/?token={key}&url={url}"
    _SOURCE = "scrape_do"

    def _api_key(self) -> str:
        return self._settings.SCRAPE_DO_API_KEY
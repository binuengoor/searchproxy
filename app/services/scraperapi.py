"""ScraperAPI service client — anti-bot firebreak."""

from __future__ import annotations

from app.services.anti_bot_base import AntiBotClient


class ScraperAPIClient(AntiBotClient):
    """Standalone async client for the ScraperAPI anti-bot service.

    Inherits credit tracking, error handling, and graceful degradation
    from :class:`AntiBotClient`. Only service-specific details are defined here.
    """

    _SERVICE_NAME = "scraperapi"
    _API_URL_TEMPLATE = "https://api.scraperapi.com/?api_key={key}&url={url}"
    _SOURCE = "scraperapi"

    def _api_key(self) -> str:
        return self._settings.SCRAPERAPI_API_KEY
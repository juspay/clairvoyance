"""Typed failures shared by website-scraping providers and API adapters."""


class WebsiteScrapingServiceError(Exception):
    """Base class for failures that are not caused by caller input."""


class WebsiteScrapingConfigurationError(WebsiteScrapingServiceError):
    """The server is missing configuration required to run a provider."""


class WebsiteScrapingUpstreamError(WebsiteScrapingServiceError):
    """The upstream provider returned an unusable response."""

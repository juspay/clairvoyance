"""What a site looks like, so the assistant can start out looking like it.

Colours, logo and icon come from a rendered read of the page (Firecrawl). It
is a starting point the merchant can change.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research.providers import (
    firecrawl,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import UnsafeUrlError
from app.core.logger import logger


async def read_branding(url: str) -> firecrawl.Branding:
    """Read the render. Never raises for a missing render: the look is empty."""
    try:
        look = await firecrawl.brand_look(url)
    except WebsiteScrapingConfigurationError as exc:
        logger.warning(f"assist brand: {exc}")
        look = firecrawl.Branding()
    # A store address too long to send on (a redirect can grow it) is no look.
    except (WebsiteScrapingUpstreamError, UnsafeUrlError) as exc:
        logger.info(f"assist brand: provider unavailable ({exc})")
        look = firecrawl.Branding()
    return look


__all__ = ["read_branding"]

"""A store's pages and what each one says, via Firecrawl ``map`` and ``scrape``.

``map`` lists a site's addresses (1 credit a call). ``scrape`` with a ``json``
format renders one page and fills a JSON schema from it (5 credits a page).
Firecrawl does the fetching, so no request to the store leaves this server.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple

import aiohttp

from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUpstreamError,
)
from app.core.config.static import FIRECRAWL_API_KEY
from app.core.transport.http_client import create_aiohttp_session

MAP_ENDPOINT = "https://api.firecrawl.dev/v2/map"
SCRAPE_ENDPOINT = "https://api.firecrawl.dev/v2/scrape"
# Seconds our request may run past the timeout Firecrawl is given.
_SLACK_SECONDS = 10.0


async def map_site(url: str, *, limit: int, timeout_seconds: float) -> List[str]:
    """Addresses Firecrawl finds on the site at ``url`` (sitemap and links)."""
    payload = {
        "url": url,
        "limit": limit,
        "includeSubdomains": False,
        "ignoreQueryParameters": True,
        "timeout": int(timeout_seconds * 1000),
    }
    parsed = await _post(MAP_ENDPOINT, payload, timeout_seconds)
    links = parsed.get("links")
    if not isinstance(links, list):
        raise WebsiteScrapingUpstreamError("provider returned no links")
    return [
        link["url"]
        for link in links
        if isinstance(link, dict) and isinstance(link.get("url"), str)
    ]


async def scrape_json(
    url: str, *, schema: Dict[str, Any], prompt: str, timeout_seconds: float
) -> Tuple[str, Dict[str, Any]]:
    """Render ``url`` and fill ``schema`` from it: (the page's final address,
    the filled object)."""
    payload = {
        "url": url,
        "formats": [{"type": "json", "schema": schema, "prompt": prompt}],
        # Contact details usually sit in the footer, which main content drops.
        "onlyMainContent": False,
        "timeout": int(timeout_seconds * 1000),
    }
    parsed = await _post(SCRAPE_ENDPOINT, payload, timeout_seconds)
    data = parsed.get("data")
    filled = data.get("json") if isinstance(data, dict) else None
    if not isinstance(filled, dict):
        raise WebsiteScrapingUpstreamError("provider returned no json block")
    metadata = data.get("metadata") if isinstance(data, dict) else None
    final = metadata.get("url") if isinstance(metadata, dict) else None
    return (final if isinstance(final, str) and final else url), filled


async def _post(
    endpoint: str, payload: Dict[str, Any], timeout_seconds: float
) -> Dict[str, Any]:
    if not FIRECRAWL_API_KEY:
        raise WebsiteScrapingConfigurationError("FIRECRAWL_API_KEY is not set")
    headers = {"Authorization": f"Bearer {FIRECRAWL_API_KEY}"}
    try:
        async with create_aiohttp_session(
            timeout=aiohttp.ClientTimeout(total=timeout_seconds + _SLACK_SECONDS)
        ) as session:
            async with session.post(endpoint, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    raise WebsiteScrapingUpstreamError(
                        f"provider returned {resp.status}"
                    )
                parsed = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise WebsiteScrapingUpstreamError(f"provider unreachable: {exc}") from exc
    except ValueError as exc:
        raise WebsiteScrapingUpstreamError("provider returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise WebsiteScrapingUpstreamError("provider returned invalid JSON")
    return parsed


__all__ = ["map_site", "scrape_json"]

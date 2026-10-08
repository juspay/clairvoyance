"""Firecrawl: a site's brand look, its pages and what each one says.

``brand_look`` reads the ``branding`` format: the browser's computed styles for
the header and primary button name a brand colour far more reliably than
counting hex values in a stylesheet. A render takes 15-40 s, so callers give it
a time budget.

``map`` lists a site's addresses (1 credit a call). ``scrape`` with a ``json``
format renders one page and fills a JSON schema from it (5 credits a page).
Firecrawl does the fetching, so no request to the store leaves this server.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit

import aiohttp

from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUnavailableError,
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    normalize_probe_url,
)
from app.core.config.static import FIRECRAWL_API_KEY
from app.core.transport.http_client import create_aiohttp_session

_MAP_ENDPOINT = "https://api.firecrawl.dev/v2/map"
_SCRAPE_ENDPOINT = "https://api.firecrawl.dev/v2/scrape"
# Longest a brand render may take.
_TIMEOUT_SECONDS = 60.0
_HEX_COLOR = re.compile(r"#(?:[0-9a-f]{3}|[0-9a-f]{6})")


class Branding(NamedTuple):
    """What the render says a site looks like; each part may be missing."""

    # "#rrggbb" or "#rgb", lower-cased.
    primary_color: Optional[str] = None
    logo_url: Optional[str] = None
    # The site's square icon: what fits a small square slot when the logo is
    # a wide wordmark.
    icon_url: Optional[str] = None


async def brand_look(url: str) -> Branding:
    """Render ``url`` and read the brand colours and logo the browser computed.

    Raises ``WebsiteScrapingConfigurationError`` without a working key and
    ``WebsiteScrapingUpstreamError`` when the provider cannot answer.
    """
    payload = {
        "url": normalize_probe_url(url),
        "formats": ["branding"],
        "timeout": int(_TIMEOUT_SECONDS * 1000),
    }
    parsed = await _call_firecrawl(_SCRAPE_ENDPOINT, payload, _TIMEOUT_SECONDS)
    try:
        return parse_branding(parsed["data"]["branding"])
    # A null or odd-shaped branding block is no answer, not a crash.
    except (KeyError, TypeError, AttributeError) as exc:
        raise WebsiteScrapingUpstreamError(
            "provider returned no branding block"
        ) from exc


def parse_branding(branding: Dict[str, Any]) -> Branding:
    """Provider payload → ``Branding``. Pure, so a captured payload replays.

    The primary colour is the store's own main button, chosen by the store and
    readable as it stands; with none, a dark site's background (a black store
    like sensesindia.in); else the render's own primary.
    """
    raw_colors = branding.get("colors") or {}
    button = (branding.get("components") or {}).get("buttonPrimary") or {}
    dark = branding.get("colorScheme") == "dark"
    # A favicon is not a logo; it is kept apart as the site's square icon.
    images = branding.get("images") or {}
    return Branding(
        primary_color=(
            _color_code(button.get("background"))
            or (_color_code(raw_colors.get("background")) if dark else None)
            or _color_code(raw_colors.get("primary"))
        ),
        logo_url=_https_url(images.get("logo")) or _https_url(branding.get("logo")),
        icon_url=_https_url(images.get("favicon")),
    )


def _color_code(value: Any) -> Optional[str]:
    """A ``#rgb`` / ``#rrggbb`` colour, lower-cased, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if _HEX_COLOR.fullmatch(text) else None


def _https_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text.startswith("https://") else None


async def list_pages(url: str, *, timeout_seconds: float) -> List[str]:
    """Addresses Firecrawl finds on the site at ``url`` (sitemap and links)."""
    payload = {
        "url": url,
        "includeSubdomains": False,
        "timeout": int(timeout_seconds * 1000),
    }
    parsed = await _call_firecrawl(_MAP_ENDPOINT, payload, timeout_seconds)
    try:
        links = parsed["links"]
    except (KeyError, TypeError) as exc:
        raise WebsiteScrapingUpstreamError("provider returned no links") from exc
    if not isinstance(links, list):
        raise WebsiteScrapingUpstreamError("provider returned no links")
    return _web_addresses(link.get("url") for link in links if isinstance(link, dict))


async def read_page(
    url: str,
    *,
    schema: Dict[str, Any],
    prompt: str,
    timeout_seconds: float,
    whole_page: bool = False,
) -> Tuple[str, Dict[str, Any], List[str]]:
    """Render ``url`` and fill ``schema`` from it: (the page's final address,
    the filled object, the page's links).

    ``whole_page`` keeps the menu and footer, where a home page keeps its
    contact details; elsewhere they only repeat the same items.
    """
    payload = {
        "url": url,
        "formats": [
            {"type": "json", "schema": schema, "prompt": prompt},
            *(["links"] if whole_page else []),
        ],
        "onlyMainContent": not whole_page,
        "timeout": int(timeout_seconds * 1000),
    }
    parsed = await _call_firecrawl(_SCRAPE_ENDPOINT, payload, timeout_seconds)
    try:
        data = parsed["data"]
        filled, links, metadata = data["json"], data.get("links"), data.get("metadata")
    except (KeyError, TypeError, AttributeError) as exc:
        raise WebsiteScrapingUpstreamError("provider returned no json block") from exc
    # A page Firecrawl could not fill counts as one page not read, not a failed run.
    if not isinstance(filled, dict):
        raise WebsiteScrapingUpstreamError("provider returned no json block")
    final = metadata.get("url") if isinstance(metadata, dict) else None
    # Where the page landed, if Firecrawl names a real address; else where we asked.
    landed = (
        final
        if isinstance(final, str)
        and _web_addresses([final])
        and urlsplit(final).hostname
        else url
    )
    return (
        landed,
        filled,
        _web_addresses(links if isinstance(links, list) else []),
    )


def _web_addresses(values: Any) -> List[str]:
    """The values that are addresses a URL parser accepts: one odd link in
    Firecrawl's answer is skipped, it does not end the run."""
    addresses: List[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            urlsplit(value)
        except ValueError:
            continue
        addresses.append(value)
    return addresses


async def _call_firecrawl(
    endpoint: str, payload: Dict[str, Any], timeout_seconds: float
) -> Dict[str, Any]:
    if not FIRECRAWL_API_KEY:
        raise WebsiteScrapingConfigurationError("FIRECRAWL_API_KEY is not set")
    headers = {"Authorization": f"Bearer {FIRECRAWL_API_KEY}"}
    try:
        async with create_aiohttp_session(
            # 10 s past Firecrawl's own limit, so its answer reaches us.
            timeout=aiohttp.ClientTimeout(total=timeout_seconds + 10)
        ) as session:
            async with session.post(endpoint, headers=headers, json=payload) as resp:
                if resp.status == 401:
                    raise WebsiteScrapingConfigurationError("provider rejected the key")
                # Out of credits, too many calls, or down: nothing about the site.
                if resp.status in (402, 429) or resp.status >= 500:
                    raise WebsiteScrapingUnavailableError(
                        f"provider returned {resp.status}"
                    )
                if resp.status != 200:
                    raise WebsiteScrapingUpstreamError(
                        f"provider returned {resp.status}"
                    )
                parsed = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise WebsiteScrapingUnavailableError(f"provider unreachable: {exc}") from exc
    except ValueError as exc:
        raise WebsiteScrapingUpstreamError("provider returned invalid JSON") from exc
    return parsed


__all__ = ["Branding", "brand_look", "list_pages", "parse_branding", "read_page"]

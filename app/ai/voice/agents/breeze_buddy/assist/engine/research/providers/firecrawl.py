"""Brand colours and logo from a rendered page, via Firecrawl's ``branding`` format.

The browser's computed styles for the header and primary button name a brand
colour far more reliably than counting hex values in a stylesheet. A render
takes 15-40 s, so callers give it a time budget.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, NamedTuple, Optional

import aiohttp

from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    normalize_probe_url,
)
from app.core.config.static import FIRECRAWL_API_KEY
from app.core.transport.http_client import create_aiohttp_session

_ENDPOINT = "https://api.firecrawl.dev/v2/scrape"
# Longest a render may take.
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

    Raises ``WebsiteScrapingConfigurationError`` without a key and
    ``WebsiteScrapingUpstreamError`` when the provider cannot answer.
    """
    if not FIRECRAWL_API_KEY:
        raise WebsiteScrapingConfigurationError("FIRECRAWL_API_KEY is not set")
    payload = {
        "url": normalize_probe_url(url),
        "formats": ["branding"],
        "timeout": int(_TIMEOUT_SECONDS * 1000),
    }
    headers = {"Authorization": f"Bearer {FIRECRAWL_API_KEY}"}
    try:
        async with create_aiohttp_session(
            # 10 s past Firecrawl's own limit, so its answer reaches us.
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS + 10)
        ) as session:
            async with session.post(_ENDPOINT, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    raise WebsiteScrapingUpstreamError(
                        f"provider returned {resp.status}"
                    )
                parsed = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise WebsiteScrapingUpstreamError(f"provider unreachable: {exc}") from exc
    except ValueError as exc:
        raise WebsiteScrapingUpstreamError("provider returned invalid JSON") from exc

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


__all__ = ["Branding", "brand_look", "parse_branding"]

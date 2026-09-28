"""Brand colours and logo from a rendered page, via Firecrawl's ``branding`` format.

The browser's computed styles for the header and primary button name a brand
colour far more reliably than counting hex values in a stylesheet. A render
takes 15-40 s, so callers give it a time budget.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Mapping, Optional, get_args

import aiohttp

from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    BrandColor,
    BrandLook,
    ColorRole,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    normalize_probe_url,
)
from app.core.config.static import FIRECRAWL_API_KEY
from app.core.transport.http_client import create_aiohttp_session

ENDPOINT = "https://api.firecrawl.dev/v2/scrape"
SOURCE = "firecrawl:branding"
# Longest a render may take when the caller sets no tighter budget.
DEFAULT_TIMEOUT_SECONDS = 60.0
# Longest logo URL kept.
_MAX_URL_LENGTH = 2048
_HEX_DIGITS = frozenset("0123456789abcdef")


async def brand_look(
    url: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> BrandLook:
    """Render ``url`` and read the brand colours and logo the browser computed.

    Raises ``WebsiteScrapingConfigurationError`` without a key and
    ``WebsiteScrapingUpstreamError`` when the provider cannot answer.
    """
    if not FIRECRAWL_API_KEY:
        raise WebsiteScrapingConfigurationError("FIRECRAWL_API_KEY is not set")
    payload = {"url": normalize_probe_url(url), "formats": ["branding"]}
    headers = {"Authorization": f"Bearer {FIRECRAWL_API_KEY}"}
    try:
        async with create_aiohttp_session(
            timeout=aiohttp.ClientTimeout(total=timeout_seconds)
        ) as session:
            async with session.post(ENDPOINT, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    raise WebsiteScrapingUpstreamError(
                        f"provider returned {resp.status}"
                    )
                parsed = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise WebsiteScrapingUpstreamError(f"provider unreachable: {exc}") from exc
    except ValueError as exc:
        raise WebsiteScrapingUpstreamError("provider returned invalid JSON") from exc

    data = parsed.get("data") if isinstance(parsed, dict) else None
    branding = data.get("branding") if isinstance(data, dict) else None
    if not isinstance(branding, dict):
        raise WebsiteScrapingUpstreamError("provider returned no branding block")
    return parse_branding(branding)


def parse_branding(branding: Dict[str, Any]) -> BrandLook:
    """Provider payload → ``BrandLook``. Pure, so a captured payload replays."""
    colors: List[BrandColor] = []
    raw_colors = branding.get("colors")
    if isinstance(raw_colors, Mapping):
        for role in get_args(ColorRole):
            value = _hex(raw_colors.get(role))
            if value:
                colors.append(BrandColor(role=role, hex=value, source=SOURCE))

    # A favicon is not a logo; it is kept apart as the site's square icon.
    images = branding.get("images")
    if not isinstance(images, Mapping):
        images = {}
    return BrandLook(
        colors=colors,
        logo_url=_https_url(images.get("logo")) or _https_url(branding.get("logo")),
        icon_url=_https_url(images.get("favicon")),
    )


def _hex(value: Any) -> Optional[str]:
    """A ``#rgb`` / ``#rrggbb`` colour, lower-cased, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if len(text) not in (4, 7) or not text.startswith("#"):
        return None
    return text if set(text[1:]) <= _HEX_DIGITS else None


def _https_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("https://") or len(text) > _MAX_URL_LENGTH:
        return None
    return text


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "brand_look", "parse_branding"]

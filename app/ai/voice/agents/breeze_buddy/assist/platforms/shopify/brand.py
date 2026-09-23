"""The merchant's own brand block, straight from the storefront API.

Authoritative when it answers, because a person typed these values into their
admin. Verified 2026-09-10 against three live stores: two returned exactly the
colours their rendered pages resolve to, and the third had never filled it in —
which is the whole reason this is one source in a chain rather than the answer.

No access token. The storefront GraphQL endpoint serves this query
unauthenticated, and it was notably *not* rate limited on stores whose HTML was.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Tuple

import aiohttp

from app.ai.voice.agents.breeze_buddy.assist.engine.models import BrandLook
from app.ai.voice.agents.breeze_buddy.assist.engine.research.brand import from_platform
from app.core.logger import logger

API_PATH = "/api/2025-01/graphql.json"
QUERY = """
{
  shop {
    name
    brand {
      slogan
      shortDescription
      colors {
        primary { background foreground }
        secondary { background foreground }
      }
      logo { image { url } }
      squareLogo { image { url } }
    }
  }
}
"""
# Colours that belong to the platform, not to any merchant: the vendor's own
# mark and its wallet button both appear on ordinary storefronts, and both look
# like a confident brand colour to anything reading pixels or stylesheets.
STOCK_COLORS: Tuple[str, ...] = (
    "#96bf48",  # the platform's own green, in "powered by" badges
    "#5a31f4",  # its accelerated-checkout button
    "#008060",  # its admin/marketing green
)
DEFAULT_TIMEOUT_SECONDS = 15.0


async def brand_look(
    host: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> Optional[BrandLook]:
    """``BrandLook`` from the storefront's brand settings, or None."""
    url = f"https://{host}{API_PATH}"
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                headers={"Content-Type": "application/json"},
                data=json.dumps({"query": QUERY}),
            ) as response:
                if response.status != 200:
                    return None
                payload = json.loads(await response.text())
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        logger.info(f"assist brand: storefront brand query failed ({exc})")
        return None

    shop = (payload.get("data") or {}).get("shop")
    if not isinstance(shop, Mapping):
        return None
    brand = shop.get("brand")
    if not isinstance(brand, Mapping):
        return None

    raw_colors = brand.get("colors")
    colors: Mapping[str, Any] = raw_colors if isinstance(raw_colors, Mapping) else {}
    look = from_platform(
        _background(colors.get("primary")), _background(colors.get("secondary"))
    )
    look.logo_url = _image(brand.get("squareLogo")) or _image(brand.get("logo"))
    return look if (look.colors or look.logo_url) else None


def _background(group: Any) -> Optional[str]:
    """First real background in the group. The API returns a list whose later
    entries are usually null placeholders for unset theme slots."""
    if not isinstance(group, list):
        return None
    for entry in group:
        if isinstance(entry, Mapping):
            value = entry.get("background")
            if isinstance(value, str) and value.startswith("#"):
                return value
    return None


def _image(node: Any) -> Optional[str]:
    if not isinstance(node, Mapping):
        return None
    image = node.get("image")
    if not isinstance(image, Mapping):
        return None
    url = image.get("url")
    return url if isinstance(url, str) and url.startswith("https://") else None


def stock_colors() -> Tuple[str, ...]:
    return STOCK_COLORS


__all__ = ["API_PATH", "QUERY", "STOCK_COLORS", "brand_look", "stock_colors"]

"""The merchant's own brand settings, from the storefront GraphQL API.

Authoritative when filled in, because the merchant typed them into their
admin; often left empty, which is why it is one source among several. The
query needs no access token.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.assist.engine.models import BrandLook
from app.ai.voice.agents.breeze_buddy.assist.engine.research.brand import from_platform
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    FetchFailedError,
    UnsafeUrlError,
    fetch_page,
)
from app.core.logger import logger

API_PATH = "/api/2025-01/graphql.json"
QUERY = """
{
  shop {
    brand {
      colors {
        primary { background }
        secondary { background }
      }
      logo { image { url } }
      squareLogo { image { url } }
    }
  }
}
"""
# The platform's own colours (its badge, its wallet button, its admin green):
# they show up on ordinary storefronts and are never the merchant's brand.
STOCK_COLORS: Tuple[str, ...] = ("#96bf48", "#5a31f4", "#008060")
_TIMEOUT_SECONDS = 10.0
_MAX_BYTES = 256 * 1024


async def brand_look(permanent_host: str) -> Optional[BrandLook]:
    """``BrandLook`` from the store's brand settings, or None.

    ``permanent_host`` must already be validated by the adapter; the request
    goes through the engine's guarded fetcher all the same.
    """
    try:
        response = await fetch_page(
            f"https://{permanent_host}{API_PATH}",
            json_body={"query": QUERY},
            headers={"Accept": "application/json"},
            timeout_seconds=_TIMEOUT_SECONDS,
            max_bytes=_MAX_BYTES,
        )
    except (UnsafeUrlError, FetchFailedError, EgressNotGuardedError) as exc:
        logger.info(f"assist brand: storefront brand query failed ({exc})")
        return None
    if response.status != 200 or response.truncated:
        return None
    try:
        payload = json.loads(response.body)
    except ValueError:
        return None

    data = payload.get("data") if isinstance(payload, Mapping) else None
    shop = data.get("shop") if isinstance(data, Mapping) else None
    brand = shop.get("brand") if isinstance(shop, Mapping) else None
    if not isinstance(brand, Mapping):
        return None
    colors = brand.get("colors")
    colors = colors if isinstance(colors, Mapping) else {}
    look = from_platform(
        _background(colors.get("primary")),
        _background(colors.get("secondary")),
        logo_url=_image(brand.get("squareLogo")) or _image(brand.get("logo")),
    )
    return look if (look.colors or look.logo_url) else None


def _background(group: Any) -> Optional[str]:
    """First set background in the group; later entries are usually null."""
    if not isinstance(group, list):
        return None
    for entry in group:
        if isinstance(entry, Mapping) and isinstance(entry.get("background"), str):
            return entry["background"]
    return None


def _image(node: Any) -> Optional[str]:
    image = node.get("image") if isinstance(node, Mapping) else None
    url = image.get("url") if isinstance(image, Mapping) else None
    return url if isinstance(url, str) and url.startswith("https://") else None


__all__ = ["API_PATH", "QUERY", "STOCK_COLORS", "brand_look"]

"""Product media resolver — fills the detail overlay's gallery when the
commerce wire ships only a single hero image (2026-07-31).

Generic seam with platform resolvers underneath (same posture as the
upsell's candidate sourcing): **UCP media is canonical and WINS whenever
it already carries a gallery** (more than one entry) — on a gateway that
ships full media this module is a pure no-op. Today's Shopify UCP gateway
projects exactly one image per product (probed live: search, get_product,
and every variant's media all carry 1), while the storefront's public
product JSON carries the merchant's full curated gallery (18 images on
the probe product). So the first resolver is the Shopify storefront
``/products/{handle}.json`` endpoint, keyed off the product's own ``url``
— no extra configuration, no platform knowledge outside this module.

No LLM anywhere: one deterministic HTTP GET per View, fail-open (any
error → the single UCP image the shopper already had), capped to the
detail projection's gallery size.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from app.core.logger import logger

# Mirrors schemas._MAX_DETAIL_IMAGES — the ProductDetailP projection caps
# there anyway; capping here too keeps the recorded binding payload lean.
MAX_GALLERY_IMAGES = 8
_FETCH_TIMEOUT_S = 3.0


def _shopify_handle_url(product_url: str) -> Optional[str]:
    """``https://host/products/{handle}[...]`` → the public product-JSON
    URL, or None when the path isn't a product page."""
    try:
        parts = urlsplit(product_url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    segments = [s for s in parts.path.split("/") if s]
    try:
        ix = segments.index("products")
    except ValueError:
        return None
    if ix + 1 >= len(segments):
        return None
    handle = segments[ix + 1]
    return f"https://{parts.netloc}/products/{handle}.json"


async def resolve_product_media(
    aiohttp_session: Any, product: Dict[str, Any]
) -> Optional[List[Dict[str, str]]]:
    """Return a gallery media list for ``product``, or ``None`` when the
    wire is already rich / nothing better exists / anything fails."""
    media = product.get("media")
    if isinstance(media, list) and len(media) > 1:
        return None  # UCP already ships a gallery — canonical data wins
    url = product.get("url")
    if not isinstance(url, str) or not url:
        return None
    json_url = _shopify_handle_url(url)
    if json_url is None or aiohttp_session is None:
        return None
    try:
        async with asyncio.timeout(_FETCH_TIMEOUT_S):
            async with aiohttp_session.get(json_url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
    except Exception:  # noqa: BLE001 — strictly fail-open decoration
        logger.warning(f"product media resolver: fetch failed for {json_url}")
        return None
    images = (
        data.get("product", {}).get("images")
        if isinstance(data, dict) and isinstance(data.get("product"), dict)
        else None
    )
    if not isinstance(images, list):
        return None
    gallery = [
        {"type": "image", "url": img["src"]}
        for img in images
        if isinstance(img, dict) and isinstance(img.get("src"), str) and img["src"]
    ][:MAX_GALLERY_IMAGES]
    # A 0/1-image gallery is no improvement over the wire's own hero.
    return gallery if len(gallery) > 1 else None


__all__ = ["resolve_product_media", "MAX_GALLERY_IMAGES"]

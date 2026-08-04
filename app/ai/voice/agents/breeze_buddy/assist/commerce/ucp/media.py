"""Product media resolution — the UCP side of the gallery seam.

**UCP media is canonical and WINS whenever it already carries a gallery**
(more than one entry): on a gateway that ships full media this module is a
pure no-op and no connector is ever consulted. Only when the wire ships a
single hero image do we ask the registered connectors whether the platform
exposes the merchant's full curated gallery somewhere else.

This module knows no platform. Resolvers live under ``connectors/`` and
register via :func:`..hooks.register_media_resolver`; with none registered
the wire's own media stands, which is the correct pure-UCP behavior.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.hooks import resolve_media

# Mirrors schemas._MAX_DETAIL_IMAGES — the ProductDetailP projection caps
# there anyway; capping here too keeps the recorded binding payload lean.
MAX_GALLERY_IMAGES = 8


async def resolve_product_media(
    aiohttp_session: Any,
    product: Dict[str, Any],
    *,
    connectors: Optional[Iterable[str]] = None,
) -> Optional[List[Dict[str, str]]]:
    """Return a gallery media list for ``product``, or ``None`` when the
    wire is already rich / no connector can do better / anything fails.

    ``connectors`` is the template's declared allowlist (empty/None = every
    registered resolver self-selects).
    """
    media = product.get("media")
    if isinstance(media, list) and len(media) > 1:
        return None  # UCP already ships a gallery — canonical data wins
    return await resolve_media(aiohttp_session, product, allowed=connectors)


__all__ = ["resolve_product_media", "MAX_GALLERY_IMAGES"]

"""The store's policies, straight from the storefront API.

Same door as the brand block and the same surprise: no access token needed. One
POST returns every policy the merchant has published, with its title, its URL,
and — usually — the whole text.

Measured on three live stores 2026-09-10, all four policies came back in full
every time: 4,211 / 1,573 / 6,656 / 24,801 characters on one, 5,110 / 4,814 /
8,929 / 7,779 on another. (An earlier run of mine reported three empty bodies
on one store; that was my query asking for `body` on only one field. Worth
recording because it nearly became a design argument.)

It still feeds a researcher rather than replacing one, for the reason a
non-platform site shows plainly: ask this of a site that is not on this
platform and it returns nothing at all. And an empty `body` is always
possible — the URLs come back either way, so whatever is missing, the loop
simply reads.

The URLs it hands back live on the vendor's own checkout host rather than the
merchant's domain. They are fetchable, but they are not an address a merchant
recognises, so the storefront path is offered alongside for display.
"""

from __future__ import annotations

import json
from typing import Any, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import aiohttp

from app.ai.voice.agents.breeze_buddy.assist.platforms.base import KnownDocument
from app.core.logger import logger

API_PATH = "/api/2025-01/graphql.json"
TIMEOUT_SECONDS = 15.0

QUERY = """
{
  shop {
    shippingPolicy { title url body }
    refundPolicy { title url body }
    privacyPolicy { title url body }
    termsOfService { title url body }
    subscriptionPolicy { title url body }
  }
}
"""

# Their field name → what the document is about, in words the engine can use
# without knowing whose API produced it.
_KINDS: Mapping[str, str] = {
    "shippingPolicy": "delivery",
    "refundPolicy": "returns",
    "privacyPolicy": "privacy",
    "termsOfService": "terms",
    "subscriptionPolicy": "subscriptions",
}
# Where the merchant's own site publishes each, so a person is shown an address
# on their own domain rather than the vendor's checkout host.
_STOREFRONT_PATHS: Mapping[str, str] = {
    "shippingPolicy": "/policies/shipping-policy",
    "refundPolicy": "/policies/refund-policy",
    "privacyPolicy": "/policies/privacy-policy",
    "termsOfService": "/policies/terms-of-service",
    "subscriptionPolicy": "/policies/subscription-policy",
}


async def known_documents(
    api_host: str, display_host: Optional[str] = None
) -> Tuple[KnownDocument, ...]:
    """Every policy this store has published. Empty tuple on any failure.

    Two hosts, because they differ and both matter. ``api_host`` is the
    store's permanent name on the platform, which is what answers the API.
    ``display_host`` is the domain its customers know. Building the shown
    address from the first labels a merchant's own returns policy as
    evidence from somebody else's website.
    """
    parts = urlsplit(api_host if "//" in api_host else f"https://{api_host}")
    if not parts.hostname:
        return ()
    endpoint = f"https://{parts.hostname}{API_PATH}"
    shown = (display_host or parts.hostname).strip().lower()

    try:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                endpoint,
                headers={"Content-Type": "application/json"},
                data=json.dumps({"query": QUERY}),
            ) as response:
                if response.status != 200:
                    return ()
                payload = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        logger.info(f"assist documents: storefront API unreachable ({exc})")
        return ()

    shop = ((payload or {}).get("data") or {}).get("shop")
    if not isinstance(shop, dict):
        return ()

    found: List[KnownDocument] = []
    for field, kind in _KINDS.items():
        block = shop.get(field)
        if not isinstance(block, Mapping):
            continue
        url = _text(block.get("url"))
        if not url:
            continue
        found.append(
            KnownDocument(
                kind=kind,
                title=_text(block.get("title")) or kind,
                url=url,
                body=_text(block.get("body")),
                display_url=f"https://{shown}{_STOREFRONT_PATHS[field]}",
            )
        )
    if found:
        filled = sum(1 for doc in found if doc.body)
        logger.info(
            f"assist documents: {len(found)} published, {filled} with text",
        )
    return tuple(found)


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


__all__ = ["API_PATH", "QUERY", "known_documents"]

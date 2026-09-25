"""A store's published policies, from the public storefront API.

One POST, no token, returns each policy's title, URL and usually its text.
It seeds the researcher rather than replacing it: a body can come back empty,
and the researcher reads that URL itself.
"""

from __future__ import annotations

import json
from typing import Any, List, Mapping, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    FetchFailedError,
    UnsafeUrlError,
    fetch_page,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.base import KnownDocument
from app.core.logger import logger

API_PATH = "/api/2025-01/graphql.json"
TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 512 * 1024

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

# API field → (document kind, where the storefront shows it on its own domain).
_POLICIES: Mapping[str, Tuple[str, str]] = {
    "shippingPolicy": ("delivery", "/policies/shipping-policy"),
    "refundPolicy": ("returns", "/policies/refund-policy"),
    "privacyPolicy": ("privacy", "/policies/privacy-policy"),
    "termsOfService": ("terms", "/policies/terms-of-service"),
    "subscriptionPolicy": ("subscriptions", "/policies/subscription-policy"),
}


async def known_documents(
    api_host: str, display_host: Optional[str] = None
) -> Tuple[KnownDocument, ...]:
    """Every policy the store at ``api_host`` publishes; empty on any failure.

    ``display_host`` is the domain shoppers know, used only for ``display_url``.
    """
    host = (api_host or "").strip().lower()
    # An IPv6 literal (no brackets once parsed) cannot be a store's API host.
    if not host or ":" in host:
        return ()
    try:
        result = await fetch_page(
            f"https://{host}{API_PATH}",
            json_body={"query": QUERY},
            headers={"Accept": "application/json"},
            timeout_seconds=TIMEOUT_SECONDS,
            max_bytes=MAX_RESPONSE_BYTES,
            max_redirects=0,
        )
    except EgressNotGuardedError:
        raise
    except (UnsafeUrlError, FetchFailedError) as exc:
        logger.info(f"assist policies: storefront API unreachable ({exc})")
        return ()
    except Exception as exc:  # the answer is the merchant's; never let it raise
        logger.info(f"assist policies: storefront API answer unusable ({exc!r})")
        return ()
    if result.status != 200 or result.truncated:
        return ()
    try:
        payload = json.loads(result.body)
    except (ValueError, RecursionError):
        return ()
    data = payload.get("data") if isinstance(payload, dict) else None
    shop = data.get("shop") if isinstance(data, dict) else None
    if not isinstance(shop, Mapping):
        return ()

    shown = (display_host or host).strip().lower()
    documents: List[KnownDocument] = []
    for field, (kind, path) in _POLICIES.items():
        block = shop.get(field)
        if not isinstance(block, Mapping):
            continue
        url = _text(block.get("url"))
        if not url:
            continue
        documents.append(
            KnownDocument(
                kind=kind,
                title=_text(block.get("title")) or kind,
                url=url,
                body=_text(block.get("body")),
                display_url=f"https://{shown}{path}",
            )
        )
    return tuple(documents)


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


__all__ = ["API_PATH", "QUERY", "known_documents"]

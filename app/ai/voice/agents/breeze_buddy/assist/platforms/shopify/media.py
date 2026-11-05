"""Shopify gallery resolver — one UCP media hook.

UCP media is canonical and wins whenever it already carries a gallery; on
a gateway that ships full media this resolver never fires. Today's Shopify
UCP gateway projects exactly one image per product (probed live: search,
get_product, and every variant's media all carry 1), while the
storefront's public product JSON carries the merchant's full curated
gallery (18 images on the probe product).

Self-selecting: the product's own ``url`` is the only input, and anything
that isn't a ``/products/{handle}`` path returns None immediately — so a
non-Shopify store loaded alongside this connector simply never matches.
No configuration, and no Shopify knowledge outside this file.

No LLM anywhere: one deterministic HTTP GET per View, fail-open (any error
→ the single UCP image the shopper already had).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.media import (
    MAX_GALLERY_IMAGES,
)
from app.core.logger import logger

_FETCH_TIMEOUT_S = 3.0
_RESOLVE_TIMEOUT_S = 1.0


def _is_public_ip(addr: str) -> bool:
    """False for anything that could reach infrastructure rather than the
    public internet — loopback, RFC1918, link-local (incl. the cloud
    metadata endpoint 169.254.169.254), CGNAT, multicast, reserved."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _handle_url(product_url: str) -> Optional[str]:
    """``https://host/products/{handle}[...]`` → the public product-JSON
    URL, or None when the path isn't a Shopify product page.

    The product ``url`` is upstream-controlled data, and this function is
    the gate on an outbound request the SERVER makes — so it is deliberately
    strict about the host, not just the path:

    - userinfo is dropped (``user:secret@host`` would otherwise be fetched
      AND logged);
    - a non-default port is refused — storefronts are :443, and an explicit
      port is how you reach an internal service;
    - an IP-literal host must be publicly routable, which rejects the cloud
      metadata endpoint and every RFC1918 address.

    A hostname that RESOLVES to a private address is caught separately (see
    :func:`_host_resolves_public`) — that needs I/O, so it can't live here.
    """
    try:
        parts = urlsplit(product_url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    try:
        host, port = parts.hostname, parts.port
    except ValueError:  # malformed port
        return None
    if not host:
        return None
    if port not in (None, 443):
        return None
    # An IP literal never belongs to a storefront; allow only public ones.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # a name — resolution is checked before the fetch
    else:
        if not _is_public_ip(host):
            return None
    segments = [s for s in parts.path.split("/") if s]
    try:
        ix = segments.index("products")
    except ValueError:
        return None
    if ix + 1 >= len(segments):
        return None
    handle = segments[ix + 1]
    return f"https://{host}/products/{handle}.json"


async def _host_resolves_public(host: str) -> bool:
    """True when every A/AAAA record for ``host`` is publicly routable.

    Closes the DNS half of the SSRF gate: ``evil.example`` resolving to
    127.0.0.1 or 169.254.169.254 passes the syntactic check in
    :func:`_handle_url` but must not be fetched. Fail CLOSED — a lookup
    that errors or times out means we don't fetch.
    """
    try:
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(_RESOLVE_TIMEOUT_S):
            infos = await loop.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except Exception:  # noqa: BLE001 — fail closed
        return False
    addrs = [info[4][0] for info in infos if info[4]]
    return bool(addrs) and all(_is_public_ip(addr) for addr in addrs)


async def resolve_gallery(
    aiohttp_session: Any, product: Dict[str, Any]
) -> Optional[List[Dict[str, str]]]:
    """Gallery for ``product`` off the storefront product JSON, or None
    when this isn't a Shopify product / nothing better exists / anything
    fails."""
    url = product.get("url")
    if not isinstance(url, str) or not url:
        return None
    json_url = _handle_url(url)
    if json_url is None or aiohttp_session is None:
        return None
    host = urlsplit(json_url).hostname or ""
    if not await _host_resolves_public(host):
        logger.warning(
            f"shopify media resolver: refusing fetch, {host!r} does not "
            "resolve to a public address"
        )
        return None
    try:
        async with asyncio.timeout(_FETCH_TIMEOUT_S):
            # No redirects: a 302 to an internal host would sidestep every
            # check above, and a storefront product JSON never needs one.
            async with aiohttp_session.get(json_url, allow_redirects=False) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
    except Exception:  # noqa: BLE001 — strictly fail-open decoration
        # json_url is already sanitized (no userinfo) by _handle_url.
        logger.warning(f"shopify media resolver: fetch failed for {json_url}")
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


__all__ = ["resolve_gallery"]

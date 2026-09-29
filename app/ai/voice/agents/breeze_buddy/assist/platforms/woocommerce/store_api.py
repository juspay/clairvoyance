"""WooCommerce Store API reads, under a store's Store API base URL
(``https://<host>/wp-json/wc/store/v1``).

The Store API is public for catalog reads (no keys), so this module only
ever sends GETs. The host comes from the request path of the MCP endpoint,
so every request passes the SSRF egress check first
(``app/core/security/ssrf.py``), redirects are not followed, and the body
read is capped.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import httpx

from app.core.security.ssrf import validate_egress_url
from app.core.transport.http_client import create_http_client

_TIMEOUT_S = 15.0
# A Store API page of 100 products is well under this.
_MAX_BODY_BYTES = 2_000_000


class ResponseTooLarge(Exception):
    """The store sent more than ``_MAX_BODY_BYTES``."""


class StoreError(Exception):
    """The store could not answer; the message says why, for the model."""


async def get(
    base: str, path: str, params: Optional[Dict[str, Any]] = None
) -> httpx.Response:
    """GET a Store API path. Raises ``SSRFError`` for a non-public host and
    ``ResponseTooLarge`` for an oversized body."""
    url = f"{base.rstrip('/')}{path}"
    await validate_egress_url(url)
    async with create_http_client(timeout=_TIMEOUT_S) as client:
        async with client.stream("GET", url, params=params) as resp:
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > _MAX_BODY_BYTES:
                    raise ResponseTooLarge(f"more than {_MAX_BODY_BYTES} bytes")
            # aiter_bytes already decoded the body.
            headers = {
                k: v
                for k, v in resp.headers.items()
                if k.lower() not in ("content-encoding", "content-length")
            }
            return httpx.Response(
                resp.status_code, headers=headers, content=bytes(body)
            )


async def get_json(
    base: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    missing_ok: bool = False,
) -> Tuple[Any, int]:
    """``(json, total_pages)``. With ``missing_ok``, a 404 is ``(None, 0)``
    (one product that does not exist); every other failure raises
    ``StoreError``."""
    try:
        resp = await get(base, path, params)
    except ResponseTooLarge as e:
        raise StoreError("The store's answer was too large to use.") from e
    if resp.status_code == 404:
        if missing_ok:
            return None, 0
        raise StoreError("The store has no WooCommerce Store API at this address.")
    if 300 <= resp.status_code < 400:
        raise StoreError(
            f"The store redirects to {resp.headers.get('location')}; "
            "use the store's final host."
        )
    if resp.status_code >= 400:
        raise StoreError(f"The store answered HTTP {resp.status_code}.")
    try:
        data = resp.json()
    except ValueError:
        raise StoreError("The store did not answer with JSON.")
    pages = str(resp.headers.get("x-wp-totalpages") or "1")
    return data, int(pages) if pages.isdigit() else 1

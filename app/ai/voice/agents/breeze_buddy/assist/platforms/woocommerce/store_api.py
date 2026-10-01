"""WooCommerce Store API reads — ``https://<host>/wp-json/wc/store/v1``.

The Store API is public for catalog reads (no keys), so the gateway only
ever sends GETs. The host comes from a template, so every request passes the
SSRF egress check first (``app/core/security/ssrf.py``).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from app.core.security.ssrf import validate_egress_url
from app.core.transport.http_client import create_http_client

_TIMEOUT_S = 15.0


async def get(
    host: str, path: str, params: Optional[Dict[str, Any]] = None
) -> httpx.Response:
    """GET a Store API path. Raises ``SSRFError`` for a non-public host."""
    url = f"https://{host}/wp-json/wc/store/v1{path}"
    await validate_egress_url(url)
    async with create_http_client(timeout=_TIMEOUT_S) as client:
        return await client.get(url, params=params)

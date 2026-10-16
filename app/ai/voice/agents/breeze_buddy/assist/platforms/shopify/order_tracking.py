"""Shopify order lookup for the UCP ``order_lookup`` seam: nautilus's WISMO
route (``WISMO_ORDER_LOOKUP_URL``) with the shared ``wismo_secret`` bearer.

The shop is the template's ``.myshopify.com`` domain: its ``merchant_id``,
else its ``secrets.shop_url``. Nautilus knows no storefront domain.
"""

from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.hooks import (
    OrderLookupUnavailable,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.config.static import WISMO_ORDER_LOOKUP_URL
from app.core.logger import logger
from app.core.transport.http_client import create_http_client

LOOKUP_TIMEOUT_SECONDS = 15


def _template_vars(context: TemplateContext) -> Dict[str, Any]:
    return getattr(context.bot, "template_vars", None) or {}


def _myshopify_host(value: Any) -> Optional[str]:
    """The ``.myshopify.com`` host in a domain or URL, or ``None``."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    host = urlparse(text if "://" in text else f"https://{text}").hostname
    return host if host and host.endswith(".myshopify.com") else None


def _shop_domain(context: TemplateContext) -> Optional[str]:
    template = getattr(context.bot, "template", None)
    # The standalone Assist app prefixes its merchant ids (shopify/tenancy.py).
    merchant = str(getattr(template, "merchant_id", "") or "").strip().lower()
    host = _myshopify_host(merchant.removeprefix("assist-"))
    if host:
        return host
    # template.secrets: the session payload can override template_vars.
    return _myshopify_host((getattr(template, "secrets", None) or {}).get("shop_url"))


async def lookup_order(
    context: TemplateContext,
    *,
    order_number: str,
    phone: Optional[str],
    email: Optional[str],
) -> Tuple[int, Any]:
    """``(status_code, body)`` from nautilus; raises OrderLookupUnavailable
    when the shop or the secret is missing, or nautilus cannot be reached."""
    shop_domain = _shop_domain(context)
    token = _template_vars(context).get("wismo_secret")
    if not shop_domain or not token:
        raise OrderLookupUnavailable(
            f"shop_domain={'set' if shop_domain else 'missing'}, "
            f"wismo_secret={'set' if token else 'missing'}"
        )
    params: Dict[str, str] = {"shopDomain": shop_domain, "orderNumber": order_number}
    if phone:
        params["phone"] = phone
    if email:
        params["email"] = email
    try:
        async with create_http_client(timeout=LOOKUP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                WISMO_ORDER_LOOKUP_URL,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise OrderLookupUnavailable(repr(exc)) from exc
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    logger.info(
        f"[shopify.lookup_order] nautilus answered {response.status_code} "
        f"for order {order_number} on {shop_domain}"
    )
    return response.status_code, body


__all__ = ["lookup_order"]

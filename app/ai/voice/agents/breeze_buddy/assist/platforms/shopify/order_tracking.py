"""Shopify order lookup for the UCP ``order_lookup`` seam.

Asks nautilus's WISMO route (the Breeze Buddy app holds the shop's Shopify
token) for one order, verified by the phone or email on it. Shop domain:
the template's ``merchant_id`` (a ``.myshopify.com`` domain), falling back
to the template's own ``secrets.shop_url``. Bearer: the shared ``wismo_secret``
credential, resolved into ``template_vars`` like every ``{placeholder}``.
The route URL is static config (``WISMO_ORDER_LOOKUP_URL``).
"""

from typing import Any, Dict, Optional, Tuple

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


def _shop_domain(context: TemplateContext) -> Optional[str]:
    template = getattr(context.bot, "template", None)
    merchant = str(getattr(template, "merchant_id", "") or "").strip()
    if "." in merchant:
        return merchant
    # template.secrets, not template_vars: a session payload can override
    # shop_url there, and the shop must not come from the client.
    shop_url = (getattr(template, "secrets", None) or {}).get("shop_url")
    return str(shop_url).strip() if shop_url else None


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

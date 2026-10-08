"""WooCommerce order lookup for the UCP ``order_lookup`` seam.

- Store: the host in the template's ``/mcp/woocommerce/<host>`` tool server,
  else its ``secrets.shop_url`` (never the session payload).
- Key: the merchant's one ``provider="woocommerce"`` credential row, whose
  ``endpoint`` must be the store.
- Order: one REST read by ID (``/wp-json/wc/v3``). No search: it scans every
  order (15-24 s on 510,000 orders), so order-numbering plugins are not
  supported.
- Identity: billing or shipping phone, else billing email. Unlike nautilus,
  a wrong phone falls back to the email.
- Tracking: the Shipment Tracking plugin API, read after the identity check;
  a missing, failed or slow read gives no tracking.
"""

import asyncio
from typing import Any, Dict, List, Optional, Tuple, cast
from urllib.parse import urlparse

import httpx

from app.ai.voice.agents.breeze_buddy.accounts import (
    AccountShapeError,
    WooCommerceAccount,
    account_from_value,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.hooks import (
    OrderLookupUnavailable,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce import store_api
from app.ai.voice.agents.breeze_buddy.mcp import in_process
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.logger import logger
from app.database.accessor.breeze_buddy.credentials import (
    get_credentials_by_merchant,
)

PROVIDER = "woocommerce"
# The order read, then the tracking read: 25 s at most together.
ORDER_DEADLINE_SECONDS = 15
TRACKING_DEADLINE_SECONDS = 10
# The 404 code for a missing order; any other 404 means no REST route.
_NO_SUCH_ORDER = "woocommerce_rest_shop_order_invalid_id"
# Unusable store answers: transport errors, bad JSON or a refused host
# (SSRFError is a ValueError), an oversized body.
_STORE_FAILURES = (httpx.HTTPError, ValueError, store_api.ResponseTooLarge)
# A tracking read that fails or runs out of time gives no tracking.
_TRACKING_FAILURES = (TimeoutError, *_STORE_FAILURES)

# Order status -> the card's fulfillment_status. Others pass through, and the
# card shows its neutral "Order update".
_FULFILLMENT = {
    "completed": "fulfilled",
    "processing": "unfulfilled",
    "on-hold": "unfulfilled",
    "pending": "unfulfilled",
    "partial-shipped": "partial",
}


def _not_found() -> Tuple[int, Dict[str, Any]]:
    return 404, {"found": False, "error": "order_not_found"}


def _mismatch() -> Tuple[int, Dict[str, Any]]:
    return 403, {"found": False, "error": "identity_mismatch"}


def _store_host(context: TemplateContext) -> Optional[str]:
    """The tool server's store host (``None`` if two differ), else the host in
    ``secrets.shop_url``."""
    mcp = getattr(context.configurations, "mcp", None)
    hosts = {
        in_process.store_host(server.url, PROVIDER)
        for server in (mcp.servers if mcp else [])
        if server.enabled
    } - {None}
    if hosts:
        return hosts.pop() if len(hosts) == 1 else None
    secrets = getattr(context.bot.template, "secrets", None) or {}
    shop_url = str(secrets.get("shop_url") or "").strip().lower()
    if not shop_url:
        return None
    return urlparse(shop_url if "://" in shop_url else f"https://{shop_url}").hostname


async def _account(context: TemplateContext, host: str) -> WooCommerceAccount:
    template = context.bot.template
    merchant_id = getattr(template, "merchant_id", None)
    rows = await get_credentials_by_merchant(
        template.reseller_id, mask=False, merchant_id=merchant_id, provider=PROVIDER
    )
    rows = [r for r in rows if r.merchant_id and r.merchant_id == merchant_id]
    if len(rows) != 1:
        # A database error also reads as 0 rows; the accessor logs it.
        raise OrderLookupUnavailable(
            f"{len(rows)} woocommerce accounts for merchant {merchant_id}"
        )
    try:
        # SHAPES maps this provider to WooCommerceAccount.
        account = cast(WooCommerceAccount, account_from_value(PROVIDER, rows[0].value))
    except AccountShapeError as exc:
        raise OrderLookupUnavailable(f"woocommerce account unusable: {exc}") from exc
    if urlparse(account.endpoint).hostname != host:
        raise OrderLookupUnavailable(f"woocommerce account is not for {host}")
    return account


def _identity_matches(
    order: Dict[str, Any], phone: Optional[str], email: Optional[str]
) -> bool:
    """A 10+ digit phone on the billing or shipping address, else the billing
    email."""
    billing = order.get("billing")
    shipping = order.get("shipping")
    if not isinstance(billing, dict):
        return False
    if phone and len(phone) >= 10:
        for address in (billing, shipping):
            if not isinstance(address, dict):
                continue
            digits = "".join(
                ch for ch in str(address.get("phone") or "") if ch.isdigit()
            )
            if digits and digits[-10:] == phone[-10:]:
                return True
    if email:
        return str(billing.get("email") or "").strip().lower() == email.lower()
    return False


def _order(
    order: Dict[str, Any], trackings: List[Dict[str, Any]], site: str
) -> Dict[str, Any]:
    """One order in nautilus's WISMO shape, which the OrderStatus card reads."""
    tracking = trackings[-1] if trackings and isinstance(trackings[-1], dict) else {}
    number = str(order.get("number") or order.get("id"))
    status = str(order.get("status") or "")
    return {
        "order_name": f"#{number}",
        "order_number": int(number) if number.isdigit() else None,
        "created_at": order.get("date_created"),
        "fulfillment_status": _FULFILLMENT.get(status, status),
        "line_items": [
            str(item["name"])
            for item in order.get("line_items") or []
            if isinstance(item, dict) and item.get("name")
        ],
        "tracking_company": tracking.get("tracking_provider")
        or tracking.get("custom_tracking_provider")
        or None,
        "tracking_number": tracking.get("tracking_number") or None,
        "tracking_url": tracking.get("tracking_link")
        or tracking.get("custom_tracking_link")
        or None,
        "shipment_status": "delivered" if status == "delivered" else None,
        "order_status_url": f"{site}/my-account/view-order/{order.get('id')}/",
    }


async def _find_order(
    rest: str, number: str, auth: Tuple[str, str]
) -> Optional[Dict[str, Any]]:
    """The order whose ID and number are both ``number``, or ``None``."""
    response = await store_api.get(rest, f"/orders/{number}", auth=auth)
    if response.status_code == 404:
        body = response.json()
        if not isinstance(body, dict) or body.get("code") != _NO_SUCH_ORDER:
            raise OrderLookupUnavailable(f"no orders route at {rest}")
        return None
    if response.status_code >= 400:
        raise OrderLookupUnavailable(f"store answered {response.status_code}")
    order = response.json()
    if isinstance(order, dict) and str(order.get("number")) == number:
        return order
    return None


async def _trackings(
    site: str, order_id: Any, auth: Tuple[str, str]
) -> List[Dict[str, Any]]:
    """The order's shipments, or ``[]`` when there are none to read."""
    try:
        async with asyncio.timeout(TRACKING_DEADLINE_SECONDS):
            response = await store_api.get(
                f"{site}/wp-json/wc-shipment-tracking/v3",
                f"/orders/{order_id}/shipment-trackings",
                auth=auth,
            )
        data = response.json() if response.status_code < 400 else []
    except _TRACKING_FAILURES as exc:
        logger.warning(f"[woocommerce.lookup_order] no tracking: {exc!r}")
        return []
    return data if isinstance(data, list) else []


async def lookup_order(
    context: TemplateContext,
    *,
    order_number: str,
    phone: Optional[str],
    email: Optional[str],
) -> Tuple[int, Any]:
    """``(status_code, body)`` in nautilus's shape; OrderLookupUnavailable
    when the store or key is not set up, or the store fails or is slow."""
    host = _store_host(context)
    if not host:
        raise OrderLookupUnavailable(
            "no store for this template: no /mcp/woocommerce/<host> tool "
            "server and no secrets.shop_url, or two servers for different stores"
        )
    account = await _account(context, host)
    # ASCII digits only: the REST route matches nothing else.
    if not (order_number.isascii() and order_number.isdigit()):
        return _not_found()
    auth = (account.consumer_key, account.consumer_secret)
    site = f"https://{host}"
    try:
        async with asyncio.timeout(ORDER_DEADLINE_SECONDS):
            order = await _find_order(f"{site}/wp-json/wc/v3", order_number, auth)
    except TimeoutError as exc:
        raise OrderLookupUnavailable(
            f"store did not answer within {ORDER_DEADLINE_SECONDS}s"
        ) from exc
    except _STORE_FAILURES as exc:
        raise OrderLookupUnavailable(repr(exc)) from exc
    if order is None:
        return _not_found()
    if not _identity_matches(order, phone, email):
        return _mismatch()
    trackings = await _trackings(site, order.get("id"), auth)
    logger.info(f"[woocommerce.lookup_order] found order {order_number} on {host}")
    return 200, {"found": True, "orders": [_order(order, trackings, site)]}


__all__ = ["lookup_order"]

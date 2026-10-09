"""WooCommerce order lookup behind the ``woocommerce_order_status`` builtin.

- Store: the host in the template's ``/mcp/woocommerce/<host>`` tool server
  (never the session payload).
- Key: the credentials row the template names in
  ``flavor.ucp.credentials.woocommerce``: a plain row (no provider) of the
  template's own merchant.
- Order: one REST read by ID (``/wp-json/wc/v3``). No search: it scans every
  order (15-24 s on 510,000 orders), so order-numbering plugins are not
  supported.
- Identity: billing or shipping phone, else billing email. Unlike nautilus,
  a wrong phone falls back to the email.
- Tracking: the Shipment Tracking plugin API, read after the identity check;
  a missing, failed or slow read gives no tracking.
"""

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce import store_api
from app.ai.voice.agents.breeze_buddy.mcp import in_process
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.logger import logger
from app.database.accessor.breeze_buddy.credentials import get_credential_by_id

PROVIDER = "woocommerce"
# The flavor block that names the credential: flavor.ucp.credentials.
FLAVOR = "ucp"


class OrderLookupUnavailable(RuntimeError):
    """The store or its key is not set up, or the store cannot answer now."""


class StoreKey(BaseModel):
    """The value of the credential row: a REST API key (read access is
    enough)."""

    model_config = ConfigDict(extra="forbid")

    consumer_key: str
    consumer_secret: str

    @field_validator("consumer_key", "consumer_secret")
    @classmethod
    def _present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("is empty")
        return value


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
    "partial-shipped": "partial",
}


def _store_host(context: TemplateContext) -> Optional[str]:
    """The host in the template's tool server, or ``None`` when there is none
    or two name different stores."""
    mcp = getattr(context.configurations, "mcp", None)
    hosts = {
        in_process.store_host(server.url, PROVIDER)
        for server in (mcp.servers if mcp else [])
        if server.enabled
    } - {None}
    return hosts.pop() if len(hosts) == 1 else None


def _credential_id(context: TemplateContext) -> Optional[str]:
    """The row the template names in ``flavor.ucp.credentials.woocommerce``."""
    flavor = getattr(context.configurations, "flavor", None) or {}
    block = flavor.get(FLAVOR)
    return (block.credentials.get(PROVIDER) if block else None) or None


async def _get_store_credentials(context: TemplateContext) -> StoreKey:
    credential_id = _credential_id(context)
    if not credential_id:
        raise OrderLookupUnavailable(
            "no flavor.ucp.credentials.woocommerce in this template"
        )
    # A plain row only (no provider), as for a pre-check: an LLM / voice
    # account's key never serves here. A database error also reads as None;
    # the accessor logs it.
    credential = await get_credential_by_id(
        credential_id, mask=False, placeholder_only=True
    )
    if credential is None or not credential.is_active:
        raise OrderLookupUnavailable(
            f"credential {credential_id} is missing, inactive or a provider account"
        )
    template = context.bot.template
    # This merchant's own row only: template variables load reseller-wide
    # and global plain rows, so a key stored there is already exposed.
    merchant_id = getattr(template, "merchant_id", None)
    if not (
        credential.reseller_id
        and credential.merchant_id
        and str(credential.reseller_id) == str(template.reseller_id)
        and str(credential.merchant_id) == str(merchant_id)
    ):
        raise OrderLookupUnavailable(
            f"credential {credential_id} is not this merchant's own row"
        )
    try:
        key = StoreKey.model_validate(credential.value or {})
    except ValidationError as exc:
        # Field names only: the error text would carry the values.
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})
        raise OrderLookupUnavailable(
            f"credential {credential_id} is not a woocommerce key: {fields}"
        ) from exc
    return key


def digits(value: Any) -> str:
    """The digits in ``value``, joined."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


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
            on_order = digits(address.get("phone"))
            if on_order and on_order[-10:] == phone[-10:]:
                return True
    if email:
        return str(billing.get("email") or "").strip().lower() == email.lower()
    return False


def _text(value: Any) -> Optional[str]:
    """A plugin field as card text, or None when empty."""
    return str(value) if value not in (None, "") else None


def _order(
    order: Dict[str, Any], trackings: List[Dict[str, Any]], site: str
) -> Dict[str, Any]:
    """One order in nautilus's WISMO shape, which the OrderStatus card reads."""
    tracking = trackings[-1] if trackings and isinstance(trackings[-1], dict) else {}
    number = str(order.get("number") or order.get("id"))
    status = str(order.get("status") or "")
    tracking_number = _text(tracking.get("tracking_number"))
    tracking_url = _text(
        tracking.get("tracking_link") or tracking.get("custom_tracking_link")
    )
    fulfillment = _FULFILLMENT.get(status, status)
    # A shipment with a number or link on a processing order means it shipped.
    if (tracking_number or tracking_url) and fulfillment == "unfulfilled":
        fulfillment = "fulfilled"
    return {
        "order_name": f"#{number}",
        "order_number": int(number) if number.isdigit() else None,
        "created_at": order.get("date_created"),
        "fulfillment_status": fulfillment,
        "line_items": [
            str(item["name"])
            for item in order.get("line_items") or []
            if isinstance(item, dict) and item.get("name")
        ],
        "tracking_company": _text(
            tracking.get("tracking_provider")
            or tracking.get("custom_tracking_provider")
        ),
        "tracking_number": tracking_number,
        "tracking_url": tracking_url,
        "shipment_status": "delivered" if status == "delivered" else None,
        "order_status_url": f"{site}/my-account/view-order/{order.get('id')}/",
    }


async def _find_order(
    rest: str, number: str, auth: Tuple[str, str]
) -> Optional[Dict[str, Any]]:
    """The order whose ID and number are both ``number``, or ``None``."""
    response = await store_api.get(rest, f"/orders/{number}", auth=auth)
    if response.status_code == 404:
        # WooCommerce's own 404 is JSON with a code; a firewall's is HTML.
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or body.get("code") != _NO_SUCH_ORDER:
            raise OrderLookupUnavailable(f"no orders route at {rest}")
        return None
    if 300 <= response.status_code < 400:
        raise OrderLookupUnavailable(
            f"store redirects to {response.headers.get('location')}"
        )
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
    # ASCII digits only: the REST route matches nothing else.
    if not (order_number.isascii() and order_number.isdigit()):
        return 404, {"found": False, "error": "order_not_found"}
    # The store's numbers have no leading zeros.
    order_number = order_number.lstrip("0")
    if not order_number:
        return 404, {"found": False, "error": "order_not_found"}
    host = _store_host(context)
    if not host:
        raise OrderLookupUnavailable(
            "no store for this template: no /mcp/woocommerce/<host> tool "
            "server, or two servers for different stores"
        )
    key = await _get_store_credentials(context)
    auth = (key.consumer_key, key.consumer_secret)
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
        return 404, {"found": False, "error": "order_not_found"}
    if not _identity_matches(order, phone, email):
        return 403, {"found": False, "error": "identity_mismatch"}
    trackings = await _trackings(site, order.get("id"), auth)
    logger.info(f"[woocommerce.lookup_order] found order {order_number} on {host}")
    return 200, {"found": True, "orders": [_order(order, trackings, site)]}


__all__ = ["OrderLookupUnavailable", "digits", "lookup_order"]

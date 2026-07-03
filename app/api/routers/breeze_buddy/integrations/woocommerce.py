"""
Everything WooCommerce-specific: signature verification, order-vs-ping
detection, the eligibility gate, and translation into the lead payload.

The lead payload is built to the SAME shape nautilus already sends to
clairvoyance for Shopify (the `order-confirmation` template's expected keys):
customer_mobile_number, customer_name, shop_name, customer_address, total_price,
items[{product_name, quantity}], shopify_order_id, payment_status. So the same
template works unchanged -- we do NOT invent a new payload shape.

Verification reuses the SAME secret + helper the service uses for
order-confirmation webhooks today (ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY +
calculate_hmac_sha256).

WooCommerce REST order shape:
https://woocommerce.github.io/woocommerce-rest-api-docs/#order-properties
"""

import hmac
from typing import Any, Dict, List, Optional

from app.core.config.static import ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY
from app.core.logger import logger
from app.core.security.sha import calculate_hmac_sha256
from app.database.queries.breeze_buddy.blacklisted_numbers import (
    normalize_phone_number,
)

# Constant reseller for WooCommerce leads (mirrors nautilus's 'BB_SHOPIFY').
RESELLER_ID = "woocommerce"
# Header WooCommerce puts its base64 HMAC-SHA256 body signature in.
SIGNATURE_HEADER = "x-wc-webhook-signature"

_COD_METHOD_IDS = {"cod", "cash on delivery"}
_TRUE = {"1", "true", "yes", "on"}


def verify_woocommerce(body: bytes, signature: str) -> bool:
    """
    WooCommerce sends `X-WC-Webhook-Signature` = base64(HMAC_SHA256(raw_body,
    secret)) -- the same scheme as our existing order-confirmation webhook.
    Verify constant-time against ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY.
    """
    if not signature or not ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY:
        return False
    try:
        expected = calculate_hmac_sha256(
            body.decode("utf-8"), ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY
        )
        return hmac.compare_digest(expected, signature)
    except Exception as e:
        logger.warning(f"WooCommerce signature verification error: {e}")
        return False


def is_woocommerce_order(payload: Dict[str, Any]) -> bool:
    """Woo posts a {webhook_id} ping with no order when a webhook is saved."""
    return bool(payload.get("id")) and isinstance(payload.get("line_items"), list)


def _phone(order: Dict[str, Any]) -> str:
    """E.164 phone, matching nautilus's formatCustomerPhone (+91). Reuses the
    existing normalizer (strips to the last 10 digits) then adds the +91 prefix;
    robust to leading zeros / already-prefixed numbers."""
    billing = order.get("billing") or {}
    shipping = order.get("shipping") or {}
    raw = billing.get("phone") or shipping.get("phone") or ""
    digits = normalize_phone_number(raw)
    return f"+91{digits}" if digits else ""


def _is_cod(order: Dict[str, Any]) -> bool:
    method = (order.get("payment_method") or "").strip().lower()
    title = (order.get("payment_method_title") or "").strip().lower()
    return method in _COD_METHOD_IDS or title in _COD_METHOD_IDS


def woocommerce_eligible(
    order: Dict[str, Any], query_params: Dict[str, str]
) -> Optional[str]:
    """
    Same per-order eligibility gate nautilus applies for Shopify
    (pickNextEligibleOrderQuery): has phone, not cancelled, COD filter. Returns
    a rejection reason, or None if the order should become a lead.
    """
    if not _phone(order):
        return "no phone number"
    if (order.get("status") or "").strip().lower() == "cancelled":
        return "order cancelled"
    filter_cod = str(query_params.get("filterCOD", "")).strip().lower() in _TRUE
    if filter_cod and not _is_cod(order):
        return "filterCOD: order is not COD"
    return None


def _name(billing: Dict[str, Any]) -> str:
    first = (billing.get("first_name") or "").strip()
    last = (billing.get("last_name") or "").strip()
    return " ".join(part for part in (first, last) if part)


def _address(order: Dict[str, Any]) -> str:
    """Flatten the shipping (fallback billing) address into a single string,
    matching nautilus's `customer_address` (one string, not a struct)."""
    src = order.get("shipping") or {}
    if not any(src.get(k) for k in ("address_1", "city", "postcode")):
        src = order.get("billing") or {}
    parts = [
        src.get("address_1"),
        src.get("address_2"),
        src.get("city"),
        src.get("state"),
        src.get("postcode"),
        src.get("country"),
    ]
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())


def _total(order: Dict[str, Any]) -> float:
    total = order.get("total")
    if total is None:
        return 0.0
    try:
        return float(total)
    except (TypeError, ValueError):
        return 0.0


def woocommerce_to_lead_payload(
    order: Dict[str, Any], shop_name: str
) -> Dict[str, Any]:
    """Translate a WooCommerce order into the nautilus order-confirmation payload."""
    raw_items: List[Dict[str, Any]] = order.get("line_items") or []
    items = [
        {
            "product_name": item.get("name") or "",
            "quantity": int(item.get("quantity") or 1),
        }
        for item in raw_items
    ]
    return {
        "customer_mobile_number": _phone(order),
        "customer_name": _name(order.get("billing") or {}) or "Customer",
        "shop_name": shop_name,
        "customer_address": _address(order),
        "total_price": _total(order),
        "items": items,
        "shopify_order_id": str(order.get("id") or order.get("number") or ""),
        "payment_status": "COD" if _is_cod(order) else "PREPAID",
    }

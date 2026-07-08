"""
WooCommerce abandoned-checkout flow.

Every abandoned checkout calls (no payment yet). Maps the checkout payload into
exactly the fields the abandoned-checkout template's expected_payload_schema
requires. Input field names vary by abandoned-cart plugin, so several common
aliases are checked — adjust the lookups here if your plugin nests data
differently.
"""

from typing import Any, Dict, Optional, Tuple

from app.core.logger import logger

from .utils import (
    TOPIC_ABANDONED_CHECKOUT,
    first,
    normalize_indian_phone,
    push_lead,
)


def summarize_cart_items(data: Dict[str, Any]) -> str:
    """Build the ``cart_items_summary`` string: "Name (x2), Other (x1)".

    Reads ``line_items`` / ``cart_contents`` (a list of item dicts). If the
    payload already carries a summary string, that is used as-is.
    """
    existing = data.get("cart_items_summary")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()

    items = data.get("line_items") or data.get("cart_contents") or []
    if isinstance(items, str):
        return items.strip()

    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("product_name")
        qty = item.get("quantity") or item.get("qty") or 1
        if name:
            parts.append(f"{name} (x{qty})")
    return ", ".join(parts)


def build_abandoned_checkout_payload(
    data: Dict[str, Any], shop_name: str
) -> Tuple[str, Dict[str, Any]]:
    """Map an abandoned-checkout webhook into (phone, lead_payload).

    Produces exactly the fields the abandoned-checkout template's
    expected_payload_schema requires: shop_name, cart_total (number),
    recovery_url, customer_name, cart_items_summary, customer_mobile_number.
    """
    billing = data.get("billing") or {}

    phone = normalize_indian_phone(
        billing.get("phone")
        or first(data, "phone", "wcf_phone_number", "customer_mobile_number")
        or ""
    )

    first_name = billing.get("first_name") or first(
        data, "first_name", "wcf_first_name"
    )
    last_name = billing.get("last_name") or first(data, "last_name", "wcf_last_name")
    customer_name = " ".join(
        part for part in [first_name, last_name] if part
    ).strip() or (first_name or "Customer")

    # cart_total must be numeric (template runs indian_number_to_speech on it).
    raw_total = first(data, "cart_total", "total", "order_total")
    cart_total: Any = 0.0
    if isinstance(raw_total, (str, int, float)):
        try:
            cart_total = float(raw_total)
        except (TypeError, ValueError):
            logger.warning(
                f"abandoned-checkout: could not parse cart_total from {raw_total!r}"
            )
    else:
        logger.warning(
            f"abandoned-checkout: missing/invalid cart_total ({raw_total!r})"
        )

    recovery_url = (
        first(
            data,
            "recovery_url",
            "checkout_url",
            "abandoned_checkout_url",
            "abandoned_cart_link",
            "url",
        )
        or ""
    )

    payload = {
        "shop_name": shop_name,
        "cart_total": cart_total,
        "recovery_url": recovery_url,
        "customer_name": customer_name,
        "cart_items_summary": summarize_cart_items(data),
        "customer_mobile_number": phone,
    }
    return phone, payload


async def process_abandoned_checkout(
    order: Dict[str, Any],
    merchant_id: str,
    reseller_id: str,
    shop_name: str,
    template: Optional[str],
    template_id: Optional[str],
) -> Dict[str, Any]:
    """Handle an abandoned-checkout webhook: always call (no payment yet)."""
    checkout_id = order.get("id") or first(
        order, "session_id", "checkout_id", "email", "phone"
    )
    if not checkout_id:
        return {"status": "ignored", "reason": "no checkout id in payload"}

    phone, payload = build_abandoned_checkout_payload(order, shop_name)
    return await push_lead(
        TOPIC_ABANDONED_CHECKOUT,
        checkout_id,
        phone,
        payload,
        merchant_id,
        reseller_id,
        template,
        template_id,
        "abandoned checkout",
    )

"""
Breeze abandoned-checkout flow.

Maps Breeze's abandoned-checkout webhook into the fields the abandonment
template requires: shop_name, cart_total, customer_name, cart_items_summary,
customer_mobile_number.

Breeze payload::

    {
      "checkout_id": "T9Y91n6GRYIFeJbwcqae8",
      "cart_token": "T9Y91n6GRYIFeJbwcqae8",
      "phone": "9090909033",
      "total_price": 5000,
      "line_items": [{"title": "WWE Championship Belt", "quantity": 1, ...}],
      "customer": {"first_name": "Aryan", "last_name": "CEM", ...},
      ...
    }

``shop_name`` is not in the payload — it comes from the merchant record
(resolved from the ``merchant_id`` in the webhook URL) and is passed in.
"""

from typing import Any, Dict, Tuple

from .utils import TOPIC_ABANDONED_CHECKOUT, normalize_indian_phone, push_lead


def summarize_cart_items(line_items: list) -> str:
    """Build the ``cart_items_summary`` string: "Name (x2), Other (x1)"."""
    parts = []
    for item in line_items:
        title = item.get("title")
        if title:
            parts.append(f"{title} (x{item.get('quantity') or 1})")
    return ", ".join(parts)


def build_abandoned_checkout_payload(
    data: Dict[str, Any], shop_name: str
) -> Tuple[str, Dict[str, Any]]:
    """Map a Breeze abandoned-checkout webhook into (phone, lead_payload)."""
    customer = data.get("customer") or {}
    phone = normalize_indian_phone(data.get("phone") or "")
    customer_name = (
        " ".join(
            p for p in [customer.get("first_name"), customer.get("last_name")] if p
        ).strip()
        or "Customer"
    )
    total_price_paise: object = data.get("total_price")
    if isinstance(total_price_paise, (int, float, str)):
        try:
            cart_total = float(total_price_paise) / 100.0
        except ValueError:
            cart_total = 0.0
    else:
        cart_total = 0.0

    payload = {
        "shop_name": shop_name,
        "cart_total": cart_total,
        "customer_name": customer_name,
        "cart_items_summary": summarize_cart_items(data.get("line_items") or []),
        "customer_mobile_number": phone,
        "recovery_url": data.get("abandoned_checkout_url") or "",
    }
    return phone, payload


async def process_abandoned_checkout(
    order: Dict[str, Any],
    merchant_id: str,
    reseller_id: str,
    shop_name: str,
    template_id: str,
) -> Dict[str, Any]:
    """Handle a Breeze abandoned-checkout webhook: always call (no payment yet)."""
    checkout_id = order.get("checkout_id") or order.get("cart_token")
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
        template_id,
        "abandoned checkout",
    )

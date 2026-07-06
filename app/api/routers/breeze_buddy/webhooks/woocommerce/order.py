"""
WooCommerce order-confirmation flow.

Decides which placed orders push a call lead (per the ?all_orders= preference)
and maps the WooCommerce order into the lead payload.
"""

from typing import Any, Dict, Optional, Tuple

from app.core.logger import logger

from .utils import TOPIC_ORDER_CONFIRMATION, push_lead


def evaluate_trigger(order: Dict[str, Any], all_orders: bool) -> Tuple[bool, str]:
    """Decide whether an order should push a lead, per the ?all_orders= preference.

    Returns (should_push, reason).
      all_orders=False — only Cash-on-Delivery orders push a lead (default).
      all_orders=True  — every order pushes a lead.
    """
    is_cod = (order.get("payment_method") or "").lower() == "cod"

    if all_orders:
        return True, "cod" if is_cod else "all"
    return (True, "cod") if is_cod else (False, "not a cod order")


def build_order_payload(
    order: Dict[str, Any], shop_name: str
) -> Tuple[str, Dict[str, Any]]:
    """Map a WooCommerce order into (phone, lead_payload).

    ``phone`` is returned separately so the caller can skip when empty.
    """
    billing = order.get("billing") or {}
    phone = (billing.get("phone") or order.get("phone") or "").strip()

    customer_name = " ".join(
        part
        for part in [
            billing.get("first_name") or order.get("first_name"),
            billing.get("last_name") or order.get("last_name"),
        ]
        if part
    ).strip()

    customer_address = ", ".join(
        part
        for part in [
            billing.get("address_1"),
            billing.get("address_2"),
            billing.get("city"),
            billing.get("state"),
            billing.get("postcode"),
        ]
        if part
    ).strip()

    # WooCommerce sends monetary amounts as strings ("199.00"); coerce to a
    # number for templates whose schema expects a numeric total_price.
    raw_total = order.get("total") or order.get("cart_total")
    total_price: Any = raw_total
    if isinstance(raw_total, (str, int, float)):
        try:
            total_price = float(raw_total)
        except (TypeError, ValueError):
            total_price = raw_total

    payload = {
        "customer_name": customer_name or (billing.get("first_name") or "Customer"),
        "customer_mobile_number": phone,
        "customer_address": customer_address,
        "shop_name": shop_name,
        "order_id": order.get("id"),
        "order_number": order.get("number"),
        "total_price": total_price,
        "currency": order.get("currency"),
        "payment_method": (order.get("payment_method") or "").lower(),
        "payment_method_title": order.get("payment_method_title"),
        "order_status": (order.get("status") or "").lower(),
        "items": [
            {
                "name": item.get("name"),
                "quantity": item.get("quantity"),
                "total": item.get("total"),
            }
            for item in (order.get("line_items") or [])
        ],
    }
    return phone, payload


async def process_order_confirmation(
    order: Dict[str, Any],
    merchant_id: str,
    reseller_id: str,
    shop_name: str,
    template: Optional[str],
    template_id: Optional[str],
    all_orders: bool,
) -> Dict[str, Any]:
    """Handle a placed-order webhook: push a lead per the ?all_orders= preference."""
    order_id = order.get("id")
    if not order_id:
        return {"status": "ignored", "reason": "no order id in payload"}

    should_push, reason = evaluate_trigger(order, all_orders)
    if not should_push:
        logger.info(f"woocommerce order {order_id} skipped: {reason}")
        return {"status": "skipped", "reason": reason}

    phone, payload = build_order_payload(order, shop_name)
    return await push_lead(
        TOPIC_ORDER_CONFIRMATION,
        order_id,
        phone,
        payload,
        merchant_id,
        reseller_id,
        template,
        template_id,
        reason,
    )

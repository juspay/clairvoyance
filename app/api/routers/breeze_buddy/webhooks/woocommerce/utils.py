"""
Shared helpers for WooCommerce webhook processing.

- topic constants (URL path segment)
- ``first``: generic nested-aware field lookup
- ``push_lead``: idempotent lead push used by both the order-confirmation and
  abandoned-checkout flows.
"""

from typing import Any, Dict, Optional

from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.types.models import PushLeadRequest
from app.api.routers.breeze_buddy.leads.handlers import push_lead_handler
from app.core.logger import logger
from app.database.accessor import get_leads_by_request_id
from app.schemas import UserInfo, UserRole

# Supported topics (URL path segment).
TOPIC_ORDER_CONFIRMATION = "order-confirmation"
TOPIC_ABANDONED_CHECKOUT = "abandoned-checkout"


def normalize_indian_phone(raw: Optional[str]) -> str:
    """Normalize a phone number to E.164 for dialing (defaults to India / +91).

    Telephony providers require E.164 (e.g. +919876543210). WooCommerce / cart
    checkouts often store a bare 10-digit number, or with spaces, a leading 0,
    or a ``91`` prefix without ``+``. Numbers already in ``+<country>...`` form
    are kept (only stripped of separators).
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if s.startswith("+"):
        return "+" + "".join(ch for ch in s[1:] if ch.isdigit())
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]  # drop trunk 0
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if len(digits) == 10:
        return "+91" + digits
    # Unknown shape — assume it already carries a country code.
    return "+" + digits


def first(data: Dict[str, Any], *keys: str) -> Optional[Any]:
    """Return the first non-empty value among the given keys (nested-aware).

    Looks at the top level and inside a nested ``other_fields`` block (used by
    some abandoned-cart plugins, e.g. WooCommerce Cart Abandonment Recovery).
    """
    other = data.get("other_fields") or {}
    for key in keys:
        val = data.get(key)
        if val:
            return val
        val = other.get(key)
        if val:
            return val
    return None


async def push_lead(
    topic: str,
    order_id: Any,
    phone: str,
    payload: Dict[str, Any],
    merchant_id: str,
    reseller_id: str,
    template: Optional[str],
    template_id: Optional[str],
    trigger_reason: str,
) -> Dict[str, Any]:
    """Idempotently push a lead through the existing lead pipeline."""
    if not phone:
        return {"status": "skipped", "reason": "no customer phone number"}

    request_id = f"woocommerce-{merchant_id}-{topic}-{order_id}"

    # Idempotency: WooCommerce re-delivers on non-2xx.
    if await get_leads_by_request_id(request_id):
        logger.info(f"woocommerce {topic} {order_id} already queued ({request_id})")
        return {"status": "duplicate", "request_id": request_id}

    service_user = UserInfo(
        id=f"woocommerce:{merchant_id}",
        username=f"woocommerce-{merchant_id}",
        role=UserRole.MERCHANT,
        reseller_ids=[reseller_id],
        merchant_ids=[merchant_id],
    )
    lead_req = PushLeadRequest(
        request_id=request_id,
        payload=payload,
        template=template,
        template_id=template_id,
        reseller_id=reseller_id,
        merchant_id=merchant_id,
    )

    # TEMP DEBUG (local testing) — remove later. Logs the exact lead about to be
    # pushed for both flows (order-confirmation + abandoned-checkout).
    logger.info(
        f"[woo-push] {topic} pushing lead: request_id={request_id} "
        f"template={template} template_id={template_id} "
        f"reseller_id={reseller_id} merchant_id={merchant_id} payload={payload}"
    )

    try:
        result = await push_lead_handler(lead_req, service_user)
    except HTTPException as e:
        # Blacklisted number is an expected, non-retryable skip.
        if e.status_code == status.HTTP_403_FORBIDDEN:
            logger.info(f"woocommerce {topic} {order_id} skipped: {e.detail}")
            return {"status": "skipped", "reason": str(e.detail)}
        # Config errors: log and 200 so WooCommerce doesn't retry forever.
        logger.error(
            f"woocommerce {topic} {order_id} push failed ({e.status_code}): {e.detail}"
        )
        return {"status": "error", "reason": str(e.detail)}

    return {
        "status": "queued",
        "request_id": request_id,
        "lead_call_tracker_id": result.get("lead_call_tracker_id"),
        "trigger": trigger_reason,
    }

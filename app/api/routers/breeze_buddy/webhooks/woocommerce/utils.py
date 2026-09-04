"""
Shared helpers for WooCommerce webhook processing.

- topic constants (URL path segment)
- ``first``: generic nested-aware field lookup
- ``push_lead``: idempotent lead push used by both the order-confirmation and
  abandoned-checkout flows.
- ``abort_backlog_leads``: cancel not-yet-dialed leads for an order.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.types.models import PushLeadRequest
from app.api.routers.breeze_buddy.leads.handlers import push_lead_handler
from app.core.logger import logger
from app.database.accessor import (
    acquire_lock_on_lead_by_id,
    get_leads_by_request_id,
    release_lock_on_lead_by_id,
    update_lead_call_completion_details,
)
from app.schemas import LeadCallStatus, UserInfo, UserRole
from app.utils.phone_number import normalize_indian_phone_for_dialing

# Supported topics (URL path segment).
TOPIC_ORDER_CONFIRMATION = "order-confirmation"
TOPIC_ABANDONED_CHECKOUT = "abandoned-checkout"


def normalize_indian_phone(raw: Optional[str]) -> str:
    """Normalize a WooCommerce number using the explicit India region."""
    return normalize_indian_phone_for_dialing(raw)


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
    template_id: str,
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
        template_id=template_id,
        reseller_id=reseller_id,
        merchant_id=merchant_id,
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


async def abort_backlog_leads(
    topic: str,
    order_id: Any,
    merchant_id: str,
    reason: str,
) -> Dict[str, Any]:
    """Abort not-yet-dialed leads for an order (e.g. the merchant cancelled it).

    Only leads still in BACKLOG *and* not yet claimed by a dispatch worker are
    aborted — claimed via the same atomic status-guarded lock the worker uses
    (``expected_status=BACKLOG``, which also requires ``is_locked=FALSE``), so a
    call already being set up or in progress is left untouched. Aborted leads are
    marked FINISHED with outcome ``ABORTED`` and the reason in ``metaData``.
    """
    request_id = f"woocommerce-{merchant_id}-{topic}-{order_id}"
    leads = await get_leads_by_request_id(request_id)
    if not leads:
        return {
            "status": "noop",
            "reason": "no leads for order",
            "request_id": request_id,
        }

    aborted: List[str] = []
    skipped: List[str] = []
    for lead in leads:
        # Atomically claim only if still BACKLOG and unlocked. If a worker already
        # owns it (call in progress) or it already finished, acquire returns None
        # and we leave the lead as-is.
        locked = await acquire_lock_on_lead_by_id(
            lead.id, expected_status=LeadCallStatus.BACKLOG
        )
        if not locked:
            skipped.append(lead.id)
            continue
        await update_lead_call_completion_details(
            id=lead.id,
            status=LeadCallStatus.FINISHED,
            outcome="ABORTED",
            meta_data={"reason": reason},
            call_end_time=datetime.now(timezone.utc),
        )
        await release_lock_on_lead_by_id(lead.id)
        aborted.append(lead.id)

    logger.info(
        f"woocommerce abort {request_id}: aborted={aborted} "
        f"left_untouched={skipped}"
    )
    return {
        "status": "aborted" if aborted else "noop",
        "request_id": request_id,
        "aborted_lead_ids": aborted,
        "skipped_lead_ids": skipped,
    }

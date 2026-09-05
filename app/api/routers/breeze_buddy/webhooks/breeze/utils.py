"""
Shared helpers for Breeze webhook processing.

- topic constant (URL path segment)
- ``normalize_indian_phone``: E.164 normalization for dialing
- ``push_lead``: idempotent lead push used by the abandoned-checkout flow.
"""

from typing import Any, Dict, Optional

from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.types.models import PushLeadRequest
from app.api.routers.breeze_buddy.leads.handlers import push_lead_handler
from app.core.logger import logger
from app.database.accessor import get_leads_by_request_id
from app.schemas import UserInfo, UserRole
from app.utils.phone_number import normalize_indian_phone_for_dialing

# Supported topics (URL path segment).
TOPIC_ABANDONED_CHECKOUT = "abandoned-checkout"


def normalize_indian_phone(raw: Optional[str]) -> str:
    """Normalize a Breeze checkout number using the explicit India region."""
    return normalize_indian_phone_for_dialing(raw)


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

    request_id = f"breeze-{merchant_id}-{topic}-{order_id}"

    # Idempotency: Breeze may re-deliver on non-2xx.
    if await get_leads_by_request_id(request_id):
        logger.info(f"breeze {topic} {order_id} already queued ({request_id})")
        return {"status": "duplicate", "request_id": request_id}

    service_user = UserInfo(
        id=f"breeze:{merchant_id}",
        username=f"breeze-{merchant_id}",
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
            logger.info(f"breeze {topic} {order_id} skipped: {e.detail}")
            return {"status": "skipped", "reason": str(e.detail)}
        # Config errors: log and 200 so Breeze doesn't retry forever.
        logger.error(
            f"breeze {topic} {order_id} push failed ({e.status_code}): {e.detail}"
        )
        return {"status": "error", "reason": str(e.detail)}

    return {
        "status": "queued",
        "request_id": request_id,
        "lead_call_tracker_id": result.get("lead_call_tracker_id"),
        "trigger": trigger_reason,
    }

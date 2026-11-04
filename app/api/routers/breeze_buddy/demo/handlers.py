"""
Handler functions for the public Breeze Buddy demo endpoint.
"""

import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from app.ai.voice.agents.breeze_buddy.services.daily.daily import start_daily_session
from app.api.routers.breeze_buddy.widget_common import client_ip
from app.core.logger import logger
from app.database.accessor import create_lead_call_tracker, handle_lead_abort
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    update_lead_call_completion_details,
)
from app.database.accessor.breeze_buddy.template import get_template_in_scope
from app.schemas import ExecutionMode, LeadCallStatus
from app.services.redis.rate_limit import check_rate_limit

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEMO_RESELLER_ID = "website-demo"
DEMO_MERCHANT_ID = "website-demo"
DEMO_TEMPLATE = "order-confirmation-demo-agent"
DEMO_RATE_LIMIT_HOURLY = 10
DEMO_RATE_LIMIT_DAILY = 50
DEMO_TTL_DAYS = 7


# --------------------------------------------------------------------------- #
# Request / Response models
# --------------------------------------------------------------------------- #


class DemoAgentEnum(str, Enum):
    """Enumerated demo agents (future-ready for more types)."""

    ORDER_CONFIRMATION = DEMO_TEMPLATE


class DemoItem(BaseModel):
    product_name: str = Field(..., max_length=120)
    quantity: int = Field(..., ge=1)


class DemoConnectRequest(BaseModel):
    agent: str = Field(..., description="Demo agent template identifier")
    customer_name: str = Field(
        ...,
        description="Customer name to personalize demo",
        max_length=120,
    )
    customer_mobile_number: str = Field(
        ...,
        description="Customer phone (string). Demo only; used by template payload.",
        max_length=32,
        pattern=r"^[+0-9\s\-()]+$",
    )
    shop_name: str = Field(
        ...,
        description="Shop name shown to the user",
        max_length=120,
    )
    total_price: str = Field(
        ...,
        description="Order total displayed in the demo",
        max_length=32,
    )
    items: List[DemoItem] = Field(
        ...,
        description="At least one item to describe the order",
        min_length=1,
        max_length=100,
    )
    language: Optional[str] = Field(
        None,
        description="Language hint (e.g., 'English'); if omitted, template defaults apply",
        max_length=64,
    )

    @field_validator("agent")
    @classmethod
    def validate_agent(cls, v: str) -> str:
        if v != DemoAgentEnum.ORDER_CONFIRMATION:
            raise ValueError("Unsupported demo agent")
        return v


class DemoConnectResponse(BaseModel):
    room_url: str
    token: str
    session_id: str
    lead_id: str
    template: str
    ttl_days: int = DEMO_TTL_DAYS


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _rate_limit(request: Request) -> None:
    """
    Per-IP limit on demo calls: DEMO_RATE_LIMIT_HOURLY an hour and
    DEMO_RATE_LIMIT_DAILY a day. Fails closed: each demo call starts a paid
    voice session, so with Redis down or unset none is started.
    Raises HTTPException 429 when over a limit.
    """
    ip = client_ip(request)
    for bucket, limit, window_seconds in (
        ("connect_hour", DEMO_RATE_LIMIT_HOURLY, 3600),
        ("connect_day", DEMO_RATE_LIMIT_DAILY, 86400),
    ):
        decision = await check_rate_limit(
            bucket=bucket,
            identifier=ip,
            limit=limit,
            window_seconds=window_seconds,
            prefix="demo",
            fail_closed=True,
        )
        if not decision.allowed:
            logger.warning(
                f"[demo] rate limit exceeded for ip={ip} "
                f"({bucket}: {decision.count}/{limit})"
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded for demo. Please try again later.",
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )


def _build_payload(body: DemoConnectRequest) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "customer_name": body.customer_name,
        "customer_mobile_number": body.customer_mobile_number,
        "shop_name": body.shop_name,
        "total_price": body.total_price,
        "items": [item.model_dump() for item in body.items],
    }
    if body.language:
        payload["language_name"] = body.language

    payload["source"] = "demo"
    payload["is_demo"] = True
    payload["demo_agent"] = body.agent
    # Set expiry timestamp for downstream cleanup
    payload["demo_expires_at"] = (
        datetime.now(timezone.utc) + timedelta(days=DEMO_TTL_DAYS)
    ).isoformat()
    return payload


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #


async def breeze_buddy_demo_connect_handler(
    request: Request, body: DemoConnectRequest
) -> Dict[str, Any]:
    """Create demo lead and start Daily session."""
    await _rate_limit(request)

    # Ensure template exists for this merchant/shop before proceeding
    # Fixed internal demo templates: exact-scope lookup (no fallback);
    # runtime template resolution elsewhere is id-only.
    template = await get_template_in_scope(
        reseller_id=DEMO_RESELLER_ID,
        merchant_id=DEMO_MERCHANT_ID,
        name=body.agent,
    )
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Demo template not configured. Please contact support.",
        )

    payload = _build_payload(body)
    lead_id = str(uuid.uuid4())
    request_id = f"demo-{lead_id}"

    lead = await create_lead_call_tracker(
        id=lead_id,
        reseller_id=DEMO_RESELLER_ID,
        template=body.agent,
        template_id=str(template.id),
        merchant_id=DEMO_MERCHANT_ID,
        next_attempt_at=None,
        payload=payload,
        attempt_count=0,
        meta_data={"is_demo": True},
        request_id=request_id,
        execution_mode=ExecutionMode.DAILY_TEST,
        status=LeadCallStatus.BACKLOG,
    )

    if not lead:
        logger.error("[demo] failed to create demo lead")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create demo lead",
        )

    try:
        session = await start_daily_session(lead_id)
    except Exception as e:
        logger.error(f"[demo] failed to start Daily session: {e}", exc_info=True)
        # Abort/cleanup the demo lead to avoid orphaned backlog entries
        try:
            await handle_lead_abort(lead_id, "demo_start_failed")
        except Exception as abort_err:
            logger.error(
                f"[demo] failed to abort lead after Daily failure: {abort_err}"
            )
            # Fallback: mark lead finished to avoid dangling BACKLOG demo leads
            try:
                await update_lead_call_completion_details(
                    id=lead_id,
                    status=LeadCallStatus.FINISHED,
                    outcome="ABORT",
                    meta_data={"cleanup": "demo_start_failed"},
                    call_end_time=datetime.now(timezone.utc),
                )
            except Exception as fallback_err:
                logger.error(
                    f"[demo] fallback cleanup failed for lead {lead_id}: {fallback_err}"
                )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to start demo session. Please try again shortly.",
        )

    return DemoConnectResponse(
        room_url=session["room_url"],
        token=session["token"],
        session_id=session["session_id"],
        lead_id=lead_id,
        template=body.agent,
    ).model_dump()

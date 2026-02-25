"""
Handler functions for the public Breeze Buddy demo endpoint.
"""

import time
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from app.ai.voice.agents.breeze_buddy.services.daily.daily import start_daily_session
from app.core.logger import logger
from app.database.accessor import create_lead_call_tracker, handle_lead_abort
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    update_lead_call_completion_details,
)
from app.database.accessor.breeze_buddy.template import get_template_by_merchant
from app.schemas import ExecutionMode, LeadCallStatus
from app.services.redis import is_redis_configured
from app.services.redis.client import get_redis_service

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEMO_MERCHANT_ID = "website-demo"
DEMO_SHOP_IDENTIFIER = "website-demo"
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


async def _rate_limit(ip: str) -> None:
    """
    Simple per-IP rate limit using Redis if configured.
    Limits: 10/hour and 50/day (best-effort).
    Raises HTTPException on limit exceed.
    """
    if not is_redis_configured():
        return

    try:
        redis = await get_redis_service()
        hour_key = f"demo:ip:{ip}:h:{int(time.time() // 3600)}"
        day_key = f"demo:ip:{ip}:d:{int(time.time() // 86400)}"

        # Hour bucket
        hour_count = await redis.incr(hour_key)
        if hour_count == 1:
            await redis.expire(hour_key, 3600)

        # Day bucket
        day_count = await redis.incr(day_key)
        if day_count == 1:
            await redis.expire(day_key, 86400)

        if hour_count >= DEMO_RATE_LIMIT_HOURLY or day_count >= DEMO_RATE_LIMIT_DAILY:
            logger.warning(
                f"[demo] rate limit exceeded for ip={ip} (hour={hour_count}, day={day_count})"
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded for demo. Please try again later.",
            )
    except HTTPException:
        raise
    except Exception as e:
        # Fail open on Redis errors but log
        logger.error(f"[demo] rate limit check failed: {e}", exc_info=True)


def _get_client_ip(request: Request) -> str:
    """
    Best-effort client IP extraction honoring common proxy headers.
    """
    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if x_forwarded_for:
        # Use the last IP in the chain (closest to us / trusted proxy)
        parts = [p.strip() for p in x_forwarded_for.split(",") if p.strip()]
        if parts:
            return parts[-1]
    x_real_ip = request.headers.get("X-Real-IP")
    if x_real_ip:
        return x_real_ip.strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


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
    client_ip = _get_client_ip(request)
    await _rate_limit(client_ip)

    # Ensure template exists for this merchant/shop before proceeding
    template = await get_template_by_merchant(
        merchant_id=DEMO_MERCHANT_ID,
        shop_identifier=DEMO_SHOP_IDENTIFIER,
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
        template_id=str(template.id),
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

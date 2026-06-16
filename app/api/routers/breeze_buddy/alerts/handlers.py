"""
Business logic for alert firing.

Responsibilities:
1. Redis SETNX dedup on alert_id (fail-open: skip dedup on Redis error)
2. Resolve alert_group_name -> phone numbers from DB (scoped by reseller_id)
3. Validate template + call_execution_config exist
4. Push one lead per phone number via create_lead_call_tracker()
5. Schedule each lead on the event-driven dispatch queue
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List

from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.dispatch.queue import schedule_lead
from app.core.logger import logger
from app.database.accessor import (
    create_lead_call_tracker,
    get_alert_group_by_name,
    get_call_execution_config_by_merchant_id,
    get_template_in_scope,
)
from app.schemas import ExecutionMode, LeadCallStatus, UserInfo
from app.services.redis.client import get_redis_service

if TYPE_CHECKING:
    from app.schemas.breeze_buddy.alerts import AlertFireRequest


def _mask_phone(phone: str) -> str:
    """Mask phone number for logging and API responses."""
    return "****"


async def _try_dedup_acquire(dedup_key: str, ttl: int) -> bool | None:
    """
    Attempt Redis SETNX for dedup.

    Uses the raw Redis client directly to avoid the service layer's
    internal exception catching (which silently converts errors to False
    and would cause fail-closed dedup suppression).

    Returns:
        True  -- key acquired (first fire in window)
        False -- key exists (duplicate, suppress)
        None  -- Redis error (fail-open: skip dedup, proceed with alert)
    """
    try:
        redis = await get_redis_service()
        client = await redis.get_client()
        acquired = await client.set(dedup_key, "1", nx=True, ex=ttl)
        return bool(acquired)
    except Exception as e:
        logger.error(f"Redis error during dedup acquire for '{dedup_key}': {e}")
        return None


async def _try_dedup_release(dedup_key: str) -> None:
    """Best-effort release of dedup key. Failures are logged, not raised."""
    try:
        redis = await get_redis_service()
        client = await redis.get_client()
        await client.delete(dedup_key)
    except Exception as e:
        logger.error(f"Redis error during dedup release for '{dedup_key}': {e}")


async def fire_alert_handler(
    req: AlertFireRequest, current_user: UserInfo, reseller_id: str
) -> Dict[str, Any]:
    """
    Core alert firing logic.

    Args:
        req: AlertFireRequest with alert parameters (no reseller_id)
        current_user: Authenticated user info (used for audit logging)
        reseller_id: Resolved from the JWT token by the router

    Returns:
        Dict with status, alert_id, leads_queued, and per-member results
    """
    dedup_key = f"bb:alert:dedup:{reseller_id}:{req.alert_id}"
    dedup_acquired = False

    logger.info(
        f"Alert fire request from user={current_user.username} "
        f"role={current_user.role.value} reseller={reseller_id} "
        f"alert_id={req.alert_id}"
    )

    # -- Step 1: Dedup (fail-open on Redis error) --------------------
    if req.dedup_ttl_seconds > 0:
        result = await _try_dedup_acquire(dedup_key, req.dedup_ttl_seconds)
        if result is False:
            logger.info(
                f"Alert '{req.alert_id}' deduplicated (TTL key exists in Redis)"
            )
            return {
                "status": "deduplicated",
                "alert_id": req.alert_id,
                "message": (
                    f"Alert suppressed — already fired within "
                    f"{req.dedup_ttl_seconds}s window"
                ),
            }
        if result is True:
            dedup_acquired = True
        # result is None => Redis error, proceed without dedup

    # -- Step 2: Resolve alert group (scoped by reseller) -------------
    group = await get_alert_group_by_name(req.alert_group_name, reseller_id)
    if not group:
        if dedup_acquired:
            await _try_dedup_release(dedup_key)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Alert group '{req.alert_group_name}' not found "
                f"for reseller '{reseller_id}'"
            ),
        )
    members: List[Dict[str, str]] = group.members
    if not members:
        if dedup_acquired:
            await _try_dedup_release(dedup_key)
        logger.warning(
            f"Alert group '{req.alert_group_name}' has no members — no calls fired"
        )
        return {
            "status": "no_members",
            "alert_id": req.alert_id,
            "alert_group_name": req.alert_group_name,
            "leads_queued": 0,
        }

    # -- Step 3: Validate template + config (fail fast) ---------------
    template = await get_template_in_scope(reseller_id, req.merchant_id, req.template)
    if not template:
        if dedup_acquired:
            await _try_dedup_release(dedup_key)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Template '{req.template}' not found for reseller '{reseller_id}'"
            ),
        )

    configs = await get_call_execution_config_by_merchant_id(
        reseller_id, req.merchant_id
    )
    config = next((c for c in (configs or []) if c.template == req.template), None)
    if not config:
        if dedup_acquired:
            await _try_dedup_release(dedup_key)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(f"Call execution config not found for template '{req.template}'"),
        )

    # -- Step 4: Push one lead per member -----------------------------
    queued_leads: List[Dict[str, str]] = []
    failed: List[Dict[str, str]] = []

    for member in members:
        phone = member.get("phone")
        name = member.get("name", "")
        masked = _mask_phone(phone) if phone else "none"

        if not phone:
            logger.warning(f"Skipping alert group member {name!r} — no phone number")
            continue

        lead_id = str(uuid.uuid4())
        request_id = f"alert-{req.alert_id}-{lead_id[:8]}"

        # extra_payload merged first so reserved keys always win
        payload: Dict[str, Any] = {
            **(req.extra_payload or {}),
            "customer_mobile_number": phone,
            "customer_name": name,
            "alert_message": req.alert_message,
            "alert_id": req.alert_id,
        }

        try:
            next_attempt_at = datetime.now(timezone.utc)
            lead = await create_lead_call_tracker(
                id=lead_id,
                reseller_id=reseller_id,
                template=req.template,
                template_id=str(template.id),
                merchant_id=req.merchant_id,
                next_attempt_at=next_attempt_at,
                payload=payload,
                attempt_count=0,
                meta_data={
                    "alert_id": req.alert_id,
                    "alert_group": req.alert_group_name,
                },
                request_id=request_id,
                execution_mode=ExecutionMode.TELEPHONY_ALERT,
                status=LeadCallStatus.BACKLOG,
            )

            if lead:
                await schedule_lead(lead_id=lead_id, next_attempt_at=next_attempt_at)
                queued_leads.append(
                    {
                        "lead_id": lead_id,
                        "phone": masked,
                        "name": name,
                    }
                )
                logger.info(
                    f"Alert lead queued: {lead_id} -> {masked} (alert={req.alert_id})"
                )
            else:
                failed.append(
                    {
                        "phone": masked,
                        "reason": "create_lead_call_tracker returned None",
                    }
                )
        except Exception as e:
            logger.error(
                f"Failed to queue alert lead for {masked}: {e}",
                exc_info=True,
            )
            failed.append({"phone": masked, "reason": str(e)})

    # Release dedup key if any leads failed so retries are not suppressed
    if failed and dedup_acquired:
        await _try_dedup_release(dedup_key)

    return {
        "status": "queued" if queued_leads else "all_failed",
        "alert_id": req.alert_id,
        "alert_group_name": req.alert_group_name,
        "reseller_id": reseller_id,
        "leads_queued": len(queued_leads),
        "leads": queued_leads,
        "failed": failed,
    }

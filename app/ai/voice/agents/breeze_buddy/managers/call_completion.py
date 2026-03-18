"""
Call completion and unanswered call handlers.
Handles the lifecycle events after a call ends.
"""

from datetime import datetime, timezone
from typing import Optional

from app.ai.voice.agents.breeze_buddy.managers.configuration import get_lead_config
from app.ai.voice.agents.breeze_buddy.managers.number import release_number
from app.ai.voice.agents.breeze_buddy.managers.retry import retry_call
from app.ai.voice.agents.breeze_buddy.services.agent_router.client import (
    safe_release_pod,
)
from app.core.logger import logger
from app.database.accessor import (
    get_lead_by_call_id,
    get_outbound_number_by_id,
    update_lead_call_completion_details,
)
from app.schemas import CallDirection, LeadCallStatus, LeadCallTracker
from app.services.redis.client import get_redis_service


async def handle_call_completion(
    call_id: str,
    outcome: str | None = None,
    call_end_time: datetime | None = None,
    meta_data: dict | None = None,
) -> Optional[LeadCallTracker]:
    """
    Handles call completion events.
    """
    logger.info(f"Call completed for call_id: {call_id} with outcome: {outcome}")
    lead = await get_lead_by_call_id(call_id)
    if not lead:
        logger.error(f"Could not find lead for call_id: {call_id}")
        return

    # Always release outbound number (including transfers — bot leaves, cleanup happens here)
    if lead.outbound_number_id:
        outbound_number = await get_outbound_number_by_id(lead.outbound_number_id)
        if outbound_number:
            await release_number(outbound_number.id, outbound_number.provider)
        else:
            logger.error(
                f"Could not find outbound number with id: {lead.outbound_number_id} to release."
            )
    else:
        logger.info(f"No outbound number id for lead: {lead.id}")

    await safe_release_pod(call_sid=call_id, reason="call_completed")

    # Check if this is a transfer — for outcome override only
    is_transfer = (
        meta_data
        and "transfer" in meta_data
        and meta_data.get("transfer", {}).get("status") == "success"
    ) or (
        lead.metaData and lead.metaData.get("transfer", {}).get("status") == "success"
    )

    config = await get_lead_config(lead)
    if not config:
        return

    # Override outcome to "transferred" for transfer calls
    if is_transfer:
        outcome = "transferred"

    updated_lead = await update_lead_call_completion_details(
        id=lead.id,
        status=LeadCallStatus.FINISHED,
        outcome=outcome,
        meta_data=meta_data,
        call_end_time=call_end_time,
    )

    # Only retry outbound calls - inbound calls should not be retried
    if (
        outcome in ["BUSY", "NO_ANSWER"]
        and lead.call_direction == CallDirection.OUTBOUND
    ):
        await retry_call(lead, config, outcome)

    return updated_lead


async def handle_unanswered_calls(call_id: str, call_status: str | None = None):
    """
    Handles unanswered call events.

    This is called when a call fails to connect (no-answer, busy, failed).
    It releases the allocated pod (if pod isolation is enabled), cleans up
    resources, and schedules a retry if configured.
    """
    logger.info(f"Handling unanswered call for call_id: {call_id}")

    # Release the allocated pod — critical for unanswered calls since
    # the WebSocket never connected, so the pod won't release itself.
    await safe_release_pod(call_sid=call_id, reason="status_unanswered")

    lead = await get_lead_by_call_id(call_id)
    if not lead:
        logger.error(f"Could not find lead for call_id: {call_id}")
        return

    # Clean up greeting audio from Redis if it exists for unanswered calls
    try:
        redis = await get_redis_service()
        greeting_key = f"greeting:{lead.id}"
        await redis.delete(greeting_key)
        logger.info(f"Deleted greeting audio from Redis for lead {lead.id}")
    except Exception as e:
        logger.warning(
            f"Failed to delete greeting audio from Redis for lead {lead.id}: {e}"
        )

    if lead.outbound_number_id:
        outbound_number = await get_outbound_number_by_id(lead.outbound_number_id)
        if outbound_number:
            await release_number(outbound_number.id, outbound_number.provider)
        else:
            logger.error(
                f"Could not find outbound number with id: {lead.outbound_number_id} to release."
            )
    else:
        logger.info(f"No outbound number id for lead: {lead.id}")

    config = await get_lead_config(lead)
    if not config:
        return

    # Determine outcome from call_status, preserving actual call result
    outcome = call_status.upper() if call_status else "NO_ANSWER"

    await update_lead_call_completion_details(
        id=lead.id,
        status=LeadCallStatus.FINISHED,
        outcome=outcome,
        meta_data={},
        call_end_time=datetime.now(timezone.utc),
    )

    # Only retry outbound calls - inbound calls should not be retried
    if lead.call_direction == CallDirection.OUTBOUND:
        await retry_call(lead, config, outcome)

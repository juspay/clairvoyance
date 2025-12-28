"""
Webhook notification utilities for call events.
"""

from datetime import datetime, timezone

from aiohttp import ClientSession

from app.ai.voice.agents.breeze_buddy.utils.common import send_webhook_with_retry
from app.core.logger import logger
from app.schemas import LeadCallTracker


async def send_call_failure_webhook(
    session: ClientSession,
    lead: LeadCallTracker,
    failure_reason: str,
):
    """
    Sends a failure webhook for a call that could not be initiated or completed.
    """
    reporting_webhook_url = (
        lead.payload.get("reporting_webhook_url") if lead.payload else None
    )

    if not reporting_webhook_url:
        return

    webhook_data = {
        "outcome": "FAILED",
        "attemptCount": lead.attempt_count + 1,
        "failureReason": failure_reason,
        "orderId": lead.request_id,
    }

    logger.info(
        f"Sending failure webhook for lead {lead.id} to {reporting_webhook_url}"
    )

    try:
        await send_webhook_with_retry(session, reporting_webhook_url, webhook_data)
    except Exception as e:
        logger.error(f"Error sending failure webhook for lead {lead.id}: {e}")


async def send_no_answer_webhook(
    session: ClientSession,
    lead: LeadCallTracker,
    outcome: str,
):
    """
    Sends a webhook when a call is not answered after all retries.
    """
    reporting_webhook_url = (
        lead.payload.get("reporting_webhook_url") if lead.payload else None
    )

    if not reporting_webhook_url:
        return

    call_duration = None
    if lead.call_initiated_time:
        call_initiated_time_utc = lead.call_initiated_time.astimezone(timezone.utc)
        call_duration = (
            datetime.now(timezone.utc) - call_initiated_time_utc
        ).total_seconds()

    summary_data = {
        "callSid": lead.call_id,
        "outcome": outcome,
        "attemptCount": lead.attempt_count + 1,
        "callDuration": call_duration,
        "orderId": lead.request_id,
    }

    try:
        success = await send_webhook_with_retry(
            session, reporting_webhook_url, summary_data, max_retries=3
        )
        if success:
            logger.info("Successfully sent call summary webhook on no_answer.")
        else:
            logger.error(
                "Failed to send call summary webhook on no_answer after all retries."
            )
    except Exception as e:
        logger.error(f"Error sending webhook on no_answer: {e}")

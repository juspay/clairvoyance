"""
Retry management for failed/unanswered calls.
Handles scheduling retries for retry scenarios.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.ai.voice.agents.breeze_buddy.managers.webhook import send_no_answer_webhook
from app.database.accessor import create_lead_call_tracker
from app.schemas import CallExecutionConfig, LeadCallTracker


async def retry_call(
    lead: LeadCallTracker, config: CallExecutionConfig, outcome: Optional[str] = None
):
    """
    Schedules a retry for a call and sends webhook for NO_ANSWER outcomes.

    Only retries outbound calls - inbound calls should not be retried.
    """
    is_last_attempt = lead.attempt_count >= config.max_retry - 1

    # Send webhook for NO_ANSWER on every attempt
    if outcome == "NO_ANSWER":
        await send_no_answer_webhook(lead, is_last_attempt)

    # Schedule retry if not the last attempt
    if not is_last_attempt:
        next_attempt_at = datetime.now(timezone.utc) + timedelta(
            seconds=config.retry_offset
        )
        await create_lead_call_tracker(
            id=str(uuid.uuid4()),
            reseller_id=lead.reseller_id,
            template=lead.template,
            merchant_id=lead.merchant_id,
            next_attempt_at=next_attempt_at,
            payload=lead.payload,
            attempt_count=lead.attempt_count + 1,
            request_id=lead.request_id,
            meta_data={},
            call_direction=lead.call_direction,  # Inherit call direction from parent lead
        )

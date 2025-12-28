"""
Call retry management utilities.
"""

import uuid
from datetime import datetime, timedelta, timezone

from app.ai.voice.agents.breeze_buddy.managers.webhook import (
    send_no_answer_webhook,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor import create_lead_call_tracker
from app.schemas import CallExecutionConfig, LeadCallTracker


async def schedule_retry(
    lead: LeadCallTracker,
    config: CallExecutionConfig,
    outcome: str | None = None,
):
    """
    Schedules a retry for a call if retries are available.
    Sends webhook notification if max retries exceeded for NO_ANSWER outcome.
    """
    if lead.attempt_count < config.max_retry - 1:
        next_attempt_at = datetime.now(timezone.utc) + timedelta(
            seconds=config.retry_offset
        )
        await create_lead_call_tracker(
            id=str(uuid.uuid4()),
            merchant_id=lead.merchant_id,
            template=lead.template,
            shop_identifier=lead.shop_identifier,
            next_attempt_at=next_attempt_at,
            payload=lead.payload,
            attempt_count=lead.attempt_count + 1,
            request_id=lead.request_id,
            meta_data={
                "use_template_flow": (
                    lead.metaData.get("use_template_flow", False)
                    if lead.metaData
                    else False
                )
            },
        )
        logger.info(
            f"Scheduled retry for lead {lead.id} (attempt {lead.attempt_count + 2}/{config.max_retry})"
        )
    else:
        logger.info(
            f"Max retries ({config.max_retry}) reached for lead {lead.id}, no further retries scheduled"
        )
        if outcome == "NO_ANSWER":
            async with create_aiohttp_session() as session:
                await send_no_answer_webhook(session, lead, outcome)

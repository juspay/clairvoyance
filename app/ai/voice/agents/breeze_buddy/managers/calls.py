"""
Call manager - Public API for handling call operations.

This module provides the main interface for:
- Processing backlog leads and initiating calls
- Handling call completion events
- Handling unanswered calls
- Managing call recordings
"""

from datetime import datetime, timezone
from typing import Optional

from app.ai.voice.agents.breeze_buddy.managers.cleanup import cleanup_stuck_leads
from app.ai.voice.agents.breeze_buddy.managers.config import get_lead_config
from app.ai.voice.agents.breeze_buddy.managers.outbound_number import (
    release_number,
)
from app.ai.voice.agents.breeze_buddy.managers.processor import process_single_lead
from app.ai.voice.agents.breeze_buddy.managers.recording import (
    update_call_recording as update_recording,
)
from app.ai.voice.agents.breeze_buddy.managers.retry import schedule_retry
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor import (
    get_lead_by_call_id,
    get_leads_based_on_status_and_next_attempt,
    get_outbound_number_by_id,
    update_lead_call_completion_details,
)
from app.schemas import LeadCallStatus, LeadCallTracker


async def process_backlog_leads():
    """
    Processes backlog leads and initiates calls.
    """
    await cleanup_stuck_leads()

    logger.info("Processing backlog leads...")
    leads = await get_leads_based_on_status_and_next_attempt(
        LeadCallStatus.BACKLOG, datetime.now(timezone.utc)
    )
    logger.info(f"Found {len(leads)} leads to process.")

    async with create_aiohttp_session() as session:
        for lead in leads:
            await process_single_lead(session, lead)


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

    outbound_number = await get_outbound_number_by_id(lead.outbound_number_id)
    if outbound_number:
        await release_number(outbound_number.id, outbound_number.provider)
    else:
        logger.error(
            f"Could not find outbound number with id: {lead.outbound_number_id} to release."
        )

    config = await get_lead_config(lead)
    if not config:
        return

    updated_lead = await update_lead_call_completion_details(
        id=lead.id,
        status=LeadCallStatus.FINISHED,
        outcome=outcome,
        meta_data=meta_data,
        call_end_time=call_end_time,
    )

    if outcome in ["BUSY", "NO_ANSWER"]:
        await schedule_retry(lead, config, outcome)

    return updated_lead


async def handle_unanswered_calls(call_id: str):
    """
    Handles unanswered call events.
    """
    logger.info(f"Handling unanswered call for call_id: {call_id}")
    lead = await get_lead_by_call_id(call_id)
    if not lead:
        logger.error(f"Could not find lead for call_id: {call_id}")
        return

    outbound_number = await get_outbound_number_by_id(lead.outbound_number_id)
    if outbound_number:
        await release_number(outbound_number.id, outbound_number.provider)
    else:
        logger.error(
            f"Could not find outbound number with id: {lead.outbound_number_id} to release."
        )

    config = await get_lead_config(lead)
    if not config:
        return

    await update_lead_call_completion_details(
        id=lead.id,
        status=LeadCallStatus.FINISHED,
        outcome="NO_ANSWER",
        meta_data={},
        call_end_time=datetime.now(timezone.utc),
    )

    await schedule_retry(lead, config, "NO_ANSWER")


async def update_call_recording(
    call_id: str, provider_recording_url: str, provider: str
):
    """
    Processes the call recording based on UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD flag.

    If flag is enabled:
        - Downloads the call recording from the provider
        - Uploads it to GCS
        - Updates the lead with the GCS URL

    If flag is disabled:
        - Stores only the provider recording URL in the database

    Args:
        call_id: The call SID
        provider_recording_url: The URL of the recording from the provider
        provider: The provider name ('twilio' or 'exotel')
    """
    await update_recording(call_id, provider_recording_url, provider)

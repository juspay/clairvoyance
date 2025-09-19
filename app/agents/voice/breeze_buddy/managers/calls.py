"""
Cron manager for handling background tasks.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.agents.voice.breeze_buddy.services.telephony.utils import get_voice_provider
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor import (
    create_lead_call_tracker,
    get_call_execution_config_by_merchant_id,
    get_lead_by_call_id,
    get_leads_based_on_status_and_next_attempt,
    get_outbound_number_based_on_status_and_provider,
    get_outbound_number_by_id,
    update_lead_call_completion_details,
    update_lead_call_details,
    update_lead_call_recording_url,
    update_outbound_number_channels,
    update_outbound_number_status,
)
from app.schemas import (
    CallExecutionConfig,
    CallProvider,
    LeadCallOutcome,
    LeadCallStatus,
    LeadCallTracker,
    OutboundNumber,
    OutboundNumberStatus,
)


async def _get_lead_config(lead: LeadCallTracker) -> Optional[CallExecutionConfig]:
    """
    Retrieves the call execution configuration for a given lead.
    """
    configs = await get_call_execution_config_by_merchant_id(lead.merchant_id)
    if not configs:
        logger.warning(
            f"No call execution config found for merchant: {lead.merchant_id}"
        )
        return None

    config = next((c for c in configs if c.workflow == lead.workflow), None)
    if not config:
        logger.warning(f"No call execution config found for workflow: {lead.workflow}")
    return config


def _is_within_calling_hours(config: CallExecutionConfig) -> bool:
    """
    Checks if the current time is within the allowed calling hours.
    """
    IST = timezone(timedelta(hours=5, minutes=30))
    current_time = datetime.now(IST).time()

    if config.call_start_time <= config.call_end_time:
        # Normal case (e.g., 09:00–17:00)
        return config.call_start_time <= current_time <= config.call_end_time
    else:
        # Overnight case (e.g., 22:00–06:00)
        return (
            current_time >= config.call_start_time
            or current_time <= config.call_end_time
        )


async def _get_available_number(provider: CallProvider) -> Optional[OutboundNumber]:
    """
    Finds an available outbound number for a given provider.
    """
    numbers = await get_outbound_number_based_on_status_and_provider(
        OutboundNumberStatus.AVAILABLE, provider
    )
    if not numbers:
        logger.warning(f"No available outbound numbers found for provider: {provider}")
        return None

    if provider == CallProvider.EXOTEL:
        for number in numbers:
            if number.channels < number.maximum_channels:
                return number
        logger.warning(f"No available channels for provider: {provider}")
        return None
    else:
        return numbers[0]


async def _acquire_number(number: OutboundNumber):
    """
    Marks an outbound number as in use.
    """
    if number.provider == CallProvider.TWILIO:
        await update_outbound_number_status(number.id, OutboundNumberStatus.IN_USE)
    elif number.provider == CallProvider.EXOTEL:
        await update_outbound_number_channels(number.id, number.channels + 1)


async def _release_number(number_id: str, provider: CallProvider):
    """
    Releases an outbound number, making it available for other calls.
    """
    if provider == CallProvider.TWILIO:
        await update_outbound_number_status(number_id, OutboundNumberStatus.AVAILABLE)
    elif provider == CallProvider.EXOTEL:
        outbound_number = await get_outbound_number_by_id(number_id)
        if outbound_number:
            await update_outbound_number_channels(
                number_id, outbound_number.channels - 1
            )


async def _retry_call(lead: LeadCallTracker, config: CallExecutionConfig):
    """
    Schedules a retry for a call.
    """
    if lead.attempt_count < config.max_retry - 1:
        next_attempt_at = datetime.now(timezone.utc) + timedelta(
            seconds=config.retry_offset
        )
        await create_lead_call_tracker(
            id=str(uuid.uuid4()),
            merchant_id=lead.merchant_id,
            workflow=lead.workflow,
            next_attempt_at=next_attempt_at,
            payload=lead.payload,
            attempt_count=lead.attempt_count + 1,
        )


async def process_backlog_leads():
    """
    Processes backlog leads and initiates calls.
    """
    logger.info("Processing backlog leads...")
    leads = await get_leads_based_on_status_and_next_attempt(
        LeadCallStatus.BACKLOG, datetime.now(timezone.utc)
    )
    logger.info(f"Found {len(leads)} leads to process.")

    async with create_aiohttp_session() as session:
        for lead in leads:
            try:
                config = await _get_lead_config(lead)
                if not config:
                    continue

                if not _is_within_calling_hours(config):
                    logger.info(
                        f"Skipping lead {lead.id} - outside calling hours. "
                        f"Current time: {datetime.now(timezone(timedelta(hours=5, minutes=30))).time()}, "
                        f"Allowed window: {config.call_start_time} - {config.call_end_time}"
                    )
                    continue

                number_to_use = await _get_available_number(config.calling_provider)
                if not number_to_use:
                    continue

                await _acquire_number(number_to_use)

                call_provider = get_voice_provider(
                    config.calling_provider.value, session
                )
                call = call_provider.make_call(
                    lead.payload.get("customer_mobile_number"), number_to_use.number
                )

                if call and call.get("sid"):
                    await update_lead_call_details(
                        lead.id,
                        LeadCallStatus.PROCESSING,
                        call.get("sid"),
                        datetime.now(timezone.utc),
                        number_to_use.id,
                    )
                else:
                    logger.error(
                        f"Failed to initiate call for lead {lead.id}. Call response: {call}"
                    )
                    await _release_number(number_to_use.id, config.calling_provider)

                    retry_calling_provider = None

                    if config.calling_provider == CallProvider.TWILIO:
                        retry_calling_provider = CallProvider.EXOTEL
                    elif config.calling_provider == CallProvider.EXOTEL:
                        retry_calling_provider = CallProvider.TWILIO

                    retry_number_to_use = await _get_available_number(
                        retry_calling_provider
                    )
                    if not retry_number_to_use:
                        continue

                    await _acquire_number(retry_number_to_use)

                    retry_call_provider = get_voice_provider(
                        retry_calling_provider.value, session
                    )
                    retry_call = retry_call_provider.make_call(
                        lead.payload.get("customer_mobile_number"),
                        retry_number_to_use.number,
                    )

                    if retry_call and retry_call.get("sid"):
                        await update_lead_call_details(
                            lead.id,
                            LeadCallStatus.PROCESSING,
                            retry_call.get("sid"),
                            datetime.now(timezone.utc),
                            retry_number_to_use.id,
                        )
                    else:
                        logger.error(
                            f"Failed to initiate retry call for lead {lead.id}. Call response: {retry_call}"
                        )
                        await _release_number(
                            retry_number_to_use.id, retry_calling_provider
                        )

            except Exception as e:
                logger.error(f"Error processing lead {lead.id}: {e}")


async def handle_call_completion(
    call_id: str,
    outcome: LeadCallOutcome,
    transcription: dict,
    call_end_time: datetime,
    updated_address: str | None = None,
):
    """
    Handles call completion events.
    """
    logger.info(f"Call completed for call_id: {call_id} with outcome: {outcome}")
    lead = await get_lead_by_call_id(call_id)
    if not lead:
        logger.error(f"Could not find lead for call_id: {call_id}")
        return

    config = await _get_lead_config(lead)
    if not config:
        return

    await _release_number(lead.outbound_number_id, config.calling_provider)

    meta_data = {"transcription": transcription}
    if updated_address:
        meta_data["updated_address"] = updated_address

    await update_lead_call_completion_details(
        id=lead.id,
        status=LeadCallStatus.FINISHED,
        outcome=outcome,
        meta_data=meta_data,
        call_end_time=call_end_time,
    )

    if outcome in [LeadCallOutcome.BUSY, LeadCallOutcome.NO_ANSWER]:
        await _retry_call(lead, config)


async def handle_unanswered_calls(call_id: str):
    """
    Handles unanswered call events.
    """
    logger.info(f"Handling unanswered call for call_id: {call_id}")
    lead = await get_lead_by_call_id(call_id)
    if not lead:
        logger.error(f"Could not find lead for call_id: {call_id}")
        return

    config = await _get_lead_config(lead)
    if not config:
        return

    await _release_number(lead.outbound_number_id, config.calling_provider)

    await update_lead_call_completion_details(
        id=lead.id,
        status=LeadCallStatus.FINISHED,
        outcome=LeadCallOutcome.NO_ANSWER,
        meta_data={},
        call_end_time=datetime.now(timezone.utc),
    )

    await _retry_call(lead, config)


async def update_call_recording(call_id: str, recording_url: str):
    """
    Updates the call recording URL for a lead.
    """
    logger.info(f"Updating call recording for call_id: {call_id}")
    lead = await get_lead_by_call_id(call_id)
    if not lead:
        logger.error(f"Could not find lead for call_id: {call_id}")
        return

    await update_lead_call_recording_url(call_id, recording_url)

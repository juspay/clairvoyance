"""
Cron manager for handling background tasks.
"""

import uuid
from datetime import datetime, timedelta, timezone

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
    CallProvider,
    LeadCallOutcome,
    LeadCallStatus,
    OutboundNumberStatus,
)


async def process_backlog_leads():
    """
    Processes backlog leads and initiates calls.
    """
    logger.info("Processing backlog leads...")
    async with create_aiohttp_session() as session:
        try:
            leads = await get_leads_based_on_status_and_next_attempt(
                LeadCallStatus.BACKLOG, datetime.now(timezone.utc)
            )
            logger.info(f"Found {len(leads)} leads to process.")
            for lead in leads:
                try:
                    logger.info(f"Processing lead: {lead.id}")
                    configs = await get_call_execution_config_by_merchant_id(
                        lead.merchant_id
                    )
                    if not configs:
                        logger.warning(
                            f"No call execution config found for merchant: {lead.merchant_id}"
                        )
                        continue

                    config = next(
                        (c for c in configs if c.workflow == lead.workflow), None
                    )
                    if not config:
                        logger.warning(
                            f"No call execution config found for workflow: {lead.workflow}"
                        )
                        continue

                    # FIRST STEP: Check if current time is within allowed calling hours (handles overnight windows)
                    IST = timezone(timedelta(hours=5, minutes=30))
                    current_time = datetime.now(IST).time()
                    if config.call_start_time <= config.call_end_time:
                        # Normal case (e.g., 09:00–17:00)
                        within_hours = (
                            config.call_start_time
                            <= current_time
                            <= config.call_end_time
                        )
                    else:
                        # Overnight case (e.g., 22:00–06:00)
                        within_hours = (
                            current_time >= config.call_start_time
                            or current_time <= config.call_end_time
                        )

                    if not within_hours:
                        logger.info(
                            f"Skipping lead {lead.id} - outside calling hours. "
                            f"Current time: {current_time}, "
                            f"Allowed window: {config.call_start_time} - {config.call_end_time}"
                        )
                        continue

                    # Only proceed if within calling hours
                    numbers = await get_outbound_number_based_on_status_and_provider(
                        OutboundNumberStatus.AVAILABLE, config.calling_provider
                    )
                    if not numbers:
                        logger.warning(
                            f"No available outbound numbers found for provider: {config.calling_provider}"
                        )
                        continue

                    number_to_use = None
                    if config.calling_provider == CallProvider.EXOTEL:
                        for number in numbers:
                            if number.channels < number.maximum_channels:
                                number_to_use = number
                                break
                    else:
                        number_to_use = numbers[0]

                    if not number_to_use:
                        logger.warning(
                            f"No available channels for provider: {config.calling_provider}"
                        )
                        continue

                    if config.calling_provider == CallProvider.TWILIO:
                        await update_outbound_number_status(
                            number_to_use.id, OutboundNumberStatus.IN_USE
                        )
                    elif config.calling_provider == CallProvider.EXOTEL:
                        await update_outbound_number_channels(
                            number_to_use.id, number_to_use.channels + 1
                        )

                    call_provider = get_voice_provider(
                        config.calling_provider.value, session
                    )
                    call = call_provider.make_call(
                        lead.payload.get("customer_mobile_number"), number_to_use.number
                    )
                    if call.get("sid") != None:
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
                        if config.calling_provider == CallProvider.TWILIO:
                            await update_outbound_number_status(
                                number_to_use.id, OutboundNumberStatus.AVAILABLE
                            )
                        elif config.calling_provider == CallProvider.EXOTEL:
                            outbound_number = await get_outbound_number_by_id(
                                number_to_use.id
                            )
                            if outbound_number:
                                await update_outbound_number_channels(
                                    number_to_use.id, outbound_number.channels - 1
                                )

                except Exception as e:
                    logger.error(f"Error processing lead {lead.id}: {e}")

        except Exception as e:
            logger.error(f"Error processing backlog leads: {e}")


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

    configs = await get_call_execution_config_by_merchant_id(lead.merchant_id)
    config = next((c for c in configs if c.workflow == lead.workflow), None)

    if config.calling_provider == CallProvider.TWILIO:
        await update_outbound_number_status(
            lead.outbound_number_id, OutboundNumberStatus.AVAILABLE
        )
    elif config.calling_provider == CallProvider.EXOTEL:
        outbound_number = await get_outbound_number_by_id(lead.outbound_number_id)
        if outbound_number:
            await update_outbound_number_channels(
                lead.outbound_number_id, outbound_number.channels - 1
            )

    await update_lead_call_completion_details(
        id=lead.id,
        status=LeadCallStatus.FINISHED,
        outcome=outcome,
        meta_data=(
            {"transcription": transcription, "updated_address": updated_address}
            if updated_address
            else {"transcription": transcription}
        ),
        call_end_time=call_end_time,
    )

    if outcome in [LeadCallOutcome.BUSY, LeadCallOutcome.NO_ANSWER]:
        configs = await get_call_execution_config_by_merchant_id(lead.merchant_id)
        if not configs:
            logger.warning(
                f"No call execution config found for merchant: {lead.merchant_id}"
            )
            return

        config = next((c for c in configs if c.workflow == lead.workflow), None)
        if not config:
            logger.warning(
                f"No call execution config found for workflow: {lead.workflow}"
            )
            return

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


async def handle_unanswered_calls(call_id: str):
    """
    Handles unanswered call events.
    """
    logger.info(f"Handling unanswered call for call_id: {call_id}")
    lead = await get_lead_by_call_id(call_id)
    if not lead:
        logger.error(f"Could not find lead for call_id: {call_id}")
        return

    configs = await get_call_execution_config_by_merchant_id(lead.merchant_id)
    config = next((c for c in configs if c.workflow == lead.workflow), None)

    if config.calling_provider.value == "TWILIO":
        await update_outbound_number_status(
            lead.outbound_number_id, OutboundNumberStatus.AVAILABLE
        )
    elif config.calling_provider.value == "EXOTEL":
        outbound_number = await get_outbound_number_by_id(lead.outbound_number_id)
        if outbound_number:
            await update_outbound_number_channels(
                lead.outbound_number_id, outbound_number.channels - 1
            )

    await update_lead_call_completion_details(
        id=lead.id,
        status=LeadCallStatus.FINISHED,
        outcome=LeadCallOutcome.NO_ANSWER,
        meta_data={},
        call_end_time=datetime.now(timezone.utc),
    )

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

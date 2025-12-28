"""
Main call processing logic for backlog leads.
"""

from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession

from app.ai.voice.agents.breeze_buddy.managers.config import (
    get_lead_config,
    is_within_calling_hours,
)
from app.ai.voice.agents.breeze_buddy.managers.outbound_number import (
    acquire_number,
    get_available_number,
    get_available_number_by_provider,
    release_number,
)
from app.ai.voice.agents.breeze_buddy.managers.webhook import (
    send_call_failure_webhook,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.utils import get_voice_provider
from app.core.logger import logger
from app.database.accessor import (
    acquire_lock_on_lead_by_id,
    get_template_by_merchant,
    release_lock_on_lead_by_id,
    update_lead_call_completion_details,
    update_lead_call_details,
)
from app.schemas import CallProvider, LeadCallStatus, LeadCallTracker


async def _initiate_call_with_provider(
    session: ClientSession,
    locked_lead: LeadCallTracker,
    number_to_use,
    config,
    use_template_flow: bool,
):
    """
    Initiates a call using the specified provider and number.
    Returns the call response.
    """
    call_provider = get_voice_provider(
        config.calling_provider.value,
        session,
        use_template_flow,
    )
    call = call_provider.make_call(
        locked_lead.payload.get("customer_mobile_number"),
        number_to_use.number,
    )
    return call


async def _handle_call_initiation_success(
    locked_lead: LeadCallTracker, call, number_to_use
):
    """
    Handles successful call initiation.
    """
    await update_lead_call_details(
        locked_lead.id,
        LeadCallStatus.PROCESSING,
        call.get("sid"),
        datetime.now(timezone.utc),
        number_to_use.id,
    )
    logger.info(
        f"Successfully initiated call for lead {locked_lead.id} with call_sid: {call.get('sid')}"
    )


async def _handle_call_initiation_failure_with_fixed_number(
    session: ClientSession,
    locked_lead: LeadCallTracker,
    number_to_use,
):
    """
    Handles call initiation failure when using a fixed outbound number (from template).
    No retry is attempted in this case.
    """
    logger.info(
        f"Not retrying call for lead {locked_lead.id} as outbound_number_id is set in template."
    )

    await release_number(number_to_use.id, number_to_use.provider)

    await update_lead_call_completion_details(
        id=locked_lead.id,
        status=LeadCallStatus.FINISHED,
        outcome="UNKNOWN",
        meta_data={
            "failure_reason": "Failed to initiate call, no retry due to fixed outbound number."
        },
        call_end_time=datetime.now(timezone.utc),
    )

    await send_call_failure_webhook(
        session,
        locked_lead,
        "Failed to initiate call, no retry due to fixed outbound number.",
    )


async def _handle_call_initiation_failure_with_retry(
    session: ClientSession,
    locked_lead: LeadCallTracker,
    number_to_use,
    config,
    use_template_flow: bool,
):
    """
    Handles call initiation failure by attempting retry with alternate provider.
    Returns True if retry was successful, False otherwise.
    """
    await release_number(number_to_use.id, number_to_use.provider)

    # Determine retry provider
    retry_calling_provider = None

    if number_to_use.provider == CallProvider.TWILIO:
        retry_calling_provider = CallProvider.EXOTEL
    elif number_to_use.provider == CallProvider.EXOTEL:
        if not config.enable_international_call:
            logger.warning(
                f"International calls disabled for merchant {locked_lead.merchant_id}. Skipping retry with Twilio."
            )
            await update_lead_call_completion_details(
                id=locked_lead.id,
                status=LeadCallStatus.FINISHED,
                outcome="UNKNOWN",
                meta_data={
                    "failure_reason": "Failed to initiate call with EXOTEL, international calling disabled."
                },
                call_end_time=datetime.now(timezone.utc),
            )

            await send_call_failure_webhook(
                session,
                locked_lead,
                "Failed to initiate call due to NCPR, international calling disabled.",
            )
            return False

        retry_calling_provider = CallProvider.TWILIO

    # Get retry number
    retry_number_to_use = await get_available_number_by_provider(retry_calling_provider)

    if not retry_number_to_use:
        logger.warning(
            f"No retry number available for provider {retry_calling_provider.value}"
        )
        return False

    await acquire_number(retry_number_to_use)

    # Attempt retry call
    retry_call_provider = get_voice_provider(
        retry_calling_provider.value,
        session,
        use_template_flow,
    )

    retry_call = retry_call_provider.make_call(
        locked_lead.payload.get("customer_mobile_number"),
        retry_number_to_use.number,
    )

    if retry_call and retry_call.get("sid"):
        await update_lead_call_details(
            locked_lead.id,
            LeadCallStatus.PROCESSING,
            retry_call.get("sid"),
            datetime.now(timezone.utc),
            retry_number_to_use.id,
        )
        logger.info(
            f"Successfully initiated retry call for lead {locked_lead.id} with provider {retry_calling_provider.value}"
        )
        return True
    else:
        logger.error(
            f"Failed to initiate retry call for lead {locked_lead.id}. Call response: {retry_call}"
        )
        await release_number(retry_number_to_use.id, retry_number_to_use.provider)

        await update_lead_call_completion_details(
            id=locked_lead.id,
            status=LeadCallStatus.FINISHED,
            outcome="UNKNOWN",
            meta_data={
                "failure_reason": f"Failed to initiate call using {retry_calling_provider.value} after {config.calling_provider.value} failed."
            },
            call_end_time=datetime.now(timezone.utc),
        )

        await send_call_failure_webhook(
            session,
            locked_lead,
            "Failed to initiate call with both providers.",
        )
        return False


async def process_single_lead(session: ClientSession, lead: LeadCallTracker):
    """
    Processes a single lead for call initiation.
    Returns True if processing was successful or should continue to next lead.
    """
    locked_lead = None
    try:
        # Try to acquire lock for this lead atomically
        locked_lead = await acquire_lock_on_lead_by_id(lead.id)
        if not locked_lead:
            logger.info(
                f"Lead {lead.id} is already locked by another process, skipping."
            )
            return True

        # Now we have exclusive access to this lead
        logger.info(f"Successfully locked lead {lead.id} for processing.")

        config = await get_lead_config(locked_lead)
        if not config:
            return True

        if not config.enable_calling:
            logger.info(
                f"Skipping lead {locked_lead.id} - calling is disabled for merchant {locked_lead.merchant_id}, template {locked_lead.template}"
            )
            return True

        if not is_within_calling_hours(config):
            logger.info(
                f"Skipping lead {locked_lead.id} - outside calling hours. "
                f"Current time: {datetime.now(timezone(timedelta(hours=5, minutes=30))).time()}, "
                f"Allowed window: {config.call_start_time} - {config.call_end_time}"
            )
            return True

        use_template_flow = (
            lead.metaData.get("use_template_flow", False) if lead.metaData else False
        )

        template = (
            await get_template_by_merchant(
                merchant_id=config.merchant_id,
                shop_identifier=config.shop_identifier,
                name=config.template,
            )
            if use_template_flow
            else None
        )

        logger.info(
            f"Lead {locked_lead.id} - use_template_flow: {use_template_flow}, template found: {template is not None}"
        )

        number_to_use = await get_available_number(config, template)
        if not number_to_use:
            return True

        await acquire_number(number_to_use)

        call = await _initiate_call_with_provider(
            session, locked_lead, number_to_use, config, use_template_flow
        )

        if call and call.get("sid"):
            await _handle_call_initiation_success(locked_lead, call, number_to_use)
        else:
            logger.error(
                f"Failed to initiate call for lead {locked_lead.id}. Call response: {call}"
            )

            # If template has fixed outbound number, don't retry
            if template and template.outbound_number_id:
                await _handle_call_initiation_failure_with_fixed_number(
                    session, locked_lead, number_to_use
                )
                return True

            # Attempt retry with alternate provider
            retry_success = await _handle_call_initiation_failure_with_retry(
                session, locked_lead, number_to_use, config, use_template_flow
            )

        return True

    except Exception as e:
        logger.error(f"Error processing lead {lead.id}: {e}")
        return True

    finally:
        if locked_lead:
            await release_lock_on_lead_by_id(locked_lead.id)

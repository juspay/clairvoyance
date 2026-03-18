"""
Managers module for call operations.

This module serves as the main entry point for call management operations.
It contains the backlog processing logic and re-exports functions from submodules.

Module Structure:
- number.py: Outbound number acquisition and release
- configuration.py: Lead configuration retrieval
- retry.py: Retry call scheduling
- webhook.py: Webhook notifications for call outcomes
- call_completion.py: Call completion and unanswered call handlers
- recording.py: Call recording upload to cloud storage
- utils.py: Utility functions (calling hours validation, greeting preparation)
"""

from datetime import datetime, timedelta, timezone

# Re-export functions from submodules for backward compatibility
from app.ai.voice.agents.breeze_buddy.managers.call_completion import (
    handle_call_completion,
    handle_unanswered_calls,
)
from app.ai.voice.agents.breeze_buddy.managers.configuration import get_lead_config
from app.ai.voice.agents.breeze_buddy.managers.number import (
    acquire_number,
    get_available_number,
    get_retry_number,
    release_number,
)
from app.ai.voice.agents.breeze_buddy.managers.pre_checks import run_pre_checks
from app.ai.voice.agents.breeze_buddy.managers.recording import update_call_recording
from app.ai.voice.agents.breeze_buddy.managers.retry import retry_call
from app.ai.voice.agents.breeze_buddy.managers.utils import (
    is_within_calling_hours,
    prepare_and_store_initial_greeting,
)
from app.ai.voice.agents.breeze_buddy.managers.webhook import (
    send_failure_webhook,
    validate_webhook_url,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.utils import get_voice_provider
from app.ai.voice.agents.breeze_buddy.utils.common import send_webhook_with_retry
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor import (
    acquire_lock_on_lead_by_id,
    get_leads_based_on_status_and_next_attempt,
    get_leads_by_status_and_time_before,
    get_outbound_number_by_id,
    get_template_by_merchant,
    is_number_blacklisted,
    release_lock_on_lead_by_id,
    update_lead_call_completion_details,
    update_lead_call_details,
)
from app.schemas import CallDirection, CallProvider, LeadCallStatus

# Export all public functions
__all__ = [
    "process_backlog_leads",
    "initiate_calls",  # Alias for backward compatibility
    "handle_call_completion",
    "handle_unanswered_calls",
    "update_call_recording",
]


async def _run_pre_checks_for_lead(config, lead, template, session) -> bool:
    """
    Run pre-checks for a lead and handle failure cases.

    Returns True if pre-checks pass (or no pre-checks configured), False otherwise.
    On failure, marks lead as FINISHED with outcome=PRECHECK_FAILED and sends webhook.
    """
    if not config.pre_checks:
        return True

    pre_check_result = await run_pre_checks(
        pre_checks=config.pre_checks,
        lead=lead,
        template=template,
        session=session,
    )

    if pre_check_result.should_proceed:
        return True

    # Pre-checks failed
    logger.info(f"Pre-checks failed for lead {lead.id}: {pre_check_result.summary()}")

    await update_lead_call_completion_details(
        id=lead.id,
        status=LeadCallStatus.FINISHED,
        outcome="PRECHECK_FAILED",
        meta_data={
            "pre_check_results": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "reason": r.reason,
                }
                for r in pre_check_result.results
            ]
        },
        call_end_time=datetime.now(timezone.utc),
    )

    # Send webhook for pre-check failure
    reporting_webhook_url = (
        lead.payload.get("reporting_webhook_url") if lead.payload else None
    )
    if reporting_webhook_url and validate_webhook_url(reporting_webhook_url):
        failed_checks = [r for r in pre_check_result.results if not r.passed]
        webhook_data = {
            "outcome": "PRECHECK_FAILED",
            "attemptCount": lead.attempt_count + 1,
            "failureReason": "; ".join(f"{r.name}: {r.reason}" for r in failed_checks),
            "orderId": lead.request_id,
        }
        try:
            await send_webhook_with_retry(session, reporting_webhook_url, webhook_data)
        except Exception as e:
            logger.error(
                f"Error sending pre-check failure webhook for lead {lead.id}: {e}"
            )

    return False


async def _cleanup_stuck_leads():
    """
    Handles leads that have timed out in the PROCESSING state.
    Marks them as finished, releases resources, and schedules retry.
    """
    logger.info("Cleaning up stuck leads...")
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    stale_leads = await get_leads_by_status_and_time_before(
        LeadCallStatus.PROCESSING, stale_time
    )

    logger.info(f"Found {len(stale_leads)} stuck leads to clean up.")

    for lead in stale_leads:
        locked_lead = None
        try:
            # Try to acquire lock on this stuck lead to prevent race conditions
            locked_lead = await acquire_lock_on_lead_by_id(lead.id)
            if not locked_lead:
                logger.info(
                    f"Stuck lead {lead.id} is already locked by another process, skipping cleanup."
                )
                continue

            logger.info(f"Successfully locked stuck lead {lead.id} for cleanup.")

            # Close the stuck record so it won't be picked again
            await update_lead_call_completion_details(
                id=locked_lead.id,
                status=LeadCallStatus.FINISHED,
                outcome="UNKNOWN",
                meta_data={"cleanup": "stuck_processing_timeout"},
                call_end_time=datetime.now(timezone.utc),
            )

            if locked_lead.outbound_number_id:
                outbound_number = await get_outbound_number_by_id(
                    locked_lead.outbound_number_id
                )
                if outbound_number:
                    await release_number(outbound_number.id, outbound_number.provider)

            # Only retry outbound calls - inbound calls should not be retried
            config = await get_lead_config(locked_lead)
            if config and locked_lead.call_direction == CallDirection.OUTBOUND:
                await retry_call(locked_lead, config)

        except Exception as e:
            logger.error(f"Error cleaning up stuck lead {lead.id}: {e}")

        finally:
            if locked_lead:
                await release_lock_on_lead_by_id(locked_lead.id)


async def _handle_fixed_outbound_failure(session, locked_lead) -> bool:
    """
    Handles failure when template has a fixed outbound number (no retry allowed).

    Returns:
        False always (call failed, no retry possible)
    """
    logger.info(
        f"Not retrying call for lead {locked_lead.id} as outbound_number_id is set in template."
    )
    await update_lead_call_completion_details(
        id=locked_lead.id,
        status=LeadCallStatus.FINISHED,
        outcome="UNKNOWN",
        meta_data={
            "failure_reason": "Failed to initiate call, no retry due to fixed outbound number."
        },
        call_end_time=datetime.now(timezone.utc),
    )
    await send_failure_webhook(
        session,
        locked_lead,
        "Failed to initiate call, no retry due to fixed outbound number.",
    )
    return False


async def _handle_international_call_disabled(session, locked_lead) -> bool:
    """
    Handles failure when international calling is disabled and EXOTEL failed.

    Returns:
        False always (call failed, no retry possible)
    """
    # Support both new and old field names for lead
    locked_lead.reseller_id
    logger.warning(
        f"International calls disabled for reseller {locked_lead.reseller_id}. Skipping retry with Twilio."
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
    await send_failure_webhook(
        session,
        locked_lead,
        "Failed to initiate call due to NCPR, international calling disabled.",
    )
    return False


def _determine_retry_provider(
    current_provider: CallProvider, config
) -> CallProvider | None:
    """
    Determines the fallback provider based on current provider.

    Returns:
        The alternate CallProvider, or None if no retry is possible
    """
    if current_provider == CallProvider.TWILIO:
        return CallProvider.EXOTEL
    elif current_provider == CallProvider.EXOTEL:
        if config.enable_international_call:
            return CallProvider.TWILIO
        return None  # International calling disabled
    return None


async def _acquire_retry_number(retry_calling_provider: CallProvider, locked_lead):
    """
    Attempts to get and acquire a retry number from the alternate provider.

    Returns:
        The acquired number, or None if unavailable
    """
    retry_number_to_use = await get_retry_number(retry_calling_provider)
    if not retry_number_to_use:
        logger.warning(
            f"No retry number available for lead {locked_lead.id} with provider {retry_calling_provider}"
        )
        return None

    retry_acquired = await acquire_number(retry_number_to_use)
    if not retry_acquired:
        logger.warning(
            f"Failed to acquire retry number {retry_number_to_use.id} for lead {locked_lead.id} - "
            "number may be at maximum capacity"
        )
        return None

    return retry_number_to_use


async def _execute_retry_call(
    session,
    locked_lead,
    retry_number_to_use,
    retry_calling_provider: CallProvider,
    config,
) -> bool:
    """
    Executes the retry call with the alternate provider.

    Returns:
        True if call initiated successfully, False otherwise
    """
    retry_call_provider = get_voice_provider(
        retry_calling_provider,
        session,
        config.telephony_config,
    )

    retry_customer_mobile = (locked_lead.payload or {}).get("customer_mobile_number")
    if not retry_customer_mobile or not isinstance(retry_customer_mobile, str):
        logger.error(f"Invalid customer_mobile_number for retry lead {locked_lead.id}")
        await release_number(retry_number_to_use.id, retry_number_to_use.provider)
        return False

    retry_call_result = retry_call_provider.make_call(
        retry_customer_mobile,
        retry_number_to_use.number,
        reseller_id=locked_lead.reseller_id,
        template_name=locked_lead.template,
    )

    if retry_call_result and retry_call_result.get("sid"):
        await update_lead_call_details(
            locked_lead.id,
            LeadCallStatus.PROCESSING,
            retry_call_result.get("sid"),
            datetime.now(timezone.utc),
            retry_number_to_use.id,
        )
        return True

    # Retry call failed
    logger.error(
        f"Failed to initiate retry call for lead {locked_lead.id}. Call response: {retry_call_result}"
    )
    await update_lead_call_completion_details(
        id=locked_lead.id,
        status=LeadCallStatus.FINISHED,
        outcome="UNKNOWN",
        meta_data={
            "failure_reason": f"Failed to initiate call using {retry_calling_provider.value} after {config.calling_provider.value} failed."
        },
        call_end_time=datetime.now(timezone.utc),
    )
    await release_number(retry_number_to_use.id, retry_number_to_use.provider)
    await send_failure_webhook(
        session,
        locked_lead,
        "Failed to initiate call with both providers.",
    )
    return False


async def _attempt_fallback_call(
    session,
    locked_lead,
    number_to_use,
    config,
    template,
):
    """
    Handles the case when the primary call initiation fails.
    Attempts retry with alternate provider or marks as failed.
    """
    await release_number(number_to_use.id, number_to_use.provider)

    # If template has fixed outbound number, don't retry
    if template and template.outbound_number_id:
        return await _handle_fixed_outbound_failure(session, locked_lead)

    # Determine retry provider
    retry_calling_provider = _determine_retry_provider(number_to_use.provider, config)

    # Check if retry is possible
    if retry_calling_provider is None:
        # Special handling for EXOTEL when international calling is disabled
        if number_to_use.provider == CallProvider.EXOTEL:
            return await _handle_international_call_disabled(session, locked_lead)
        # For other providers (like PLIVO), no retry available - just return
        return False

    # Try to acquire a retry number
    retry_number_to_use = await _acquire_retry_number(
        retry_calling_provider, locked_lead
    )
    if not retry_number_to_use:
        return False

    # Execute the retry call
    return await _execute_retry_call(
        session,
        locked_lead,
        retry_number_to_use,
        retry_calling_provider,
        config,
    )


async def process_backlog_leads():
    """
    Processes backlog leads and initiates calls.
    """
    await _cleanup_stuck_leads()

    logger.info("Processing backlog leads...")
    leads = await get_leads_based_on_status_and_next_attempt(
        LeadCallStatus.BACKLOG, datetime.now(timezone.utc)
    )
    logger.info(f"Found {len(leads)} leads to process.")

    async with create_aiohttp_session() as session:
        for lead in leads:
            locked_lead = None
            number_to_use = None
            number_acquired = False
            call_initiated = False  # Track if call was successfully initiated
            try:
                # Try to acquire lock for this lead atomically
                locked_lead = await acquire_lock_on_lead_by_id(lead.id)
                if not locked_lead:
                    logger.info(
                        f"Lead {lead.id} is already locked by another process, skipping."
                    )
                    continue

                # Now we have exclusive access to this lead
                logger.info(f"Successfully locked lead {lead.id} for processing.")

                config = await get_lead_config(locked_lead)
                if not config:
                    continue

                if not config.enable_calling:
                    logger.info(
                        f"Skipping lead {locked_lead.id} - calling is disabled for reseller {locked_lead.reseller_id}, template {locked_lead.template}"
                    )
                    continue

                # Check if customer phone number is blacklisted
                blacklist_phone = (locked_lead.payload or {}).get(
                    "customer_mobile_number"
                )
                if blacklist_phone and await is_number_blacklisted(
                    blacklist_phone, locked_lead.reseller_id
                ):
                    logger.info(
                        f"Skipping lead {locked_lead.id} - phone number {blacklist_phone} is blacklisted"
                    )
                    await update_lead_call_completion_details(
                        id=locked_lead.id,
                        status=LeadCallStatus.FINISHED,
                        outcome="BLACKLISTED",
                        meta_data={"reason": "Phone number is blacklisted"},
                        call_end_time=datetime.now(timezone.utc),
                    )
                    continue

                if not is_within_calling_hours(config):
                    logger.info(
                        f"Skipping lead {locked_lead.id} - outside calling hours. "
                        f"Current time: {datetime.now(timezone(timedelta(hours=5, minutes=30))).time()}, "
                        f"Allowed window: {config.call_start_time} - {config.call_end_time}"
                    )
                    continue

                template = await get_template_by_merchant(
                    reseller_id=config.reseller_id,
                    merchant_id=config.merchant_id,
                    name=config.template,
                )

                logger.info(
                    f"Lead {locked_lead.id} , template found: {template is not None}"
                )

                # Run pre-checks before committing resources
                if not await _run_pre_checks_for_lead(
                    config, locked_lead, template, session
                ):
                    continue

                # Synthesize initial greeting audio and store in Redis
                if template:
                    await prepare_and_store_initial_greeting(
                        lead_id=locked_lead.id,
                        payload=locked_lead.payload or {},
                        template=template,
                    )

                number_to_use = await get_available_number(config, template)
                if not number_to_use:
                    continue

                number_acquired = await acquire_number(number_to_use)
                if not number_acquired:
                    logger.warning(
                        f"Failed to acquire number {number_to_use.id} for lead {locked_lead.id} - "
                        "number may be at maximum capacity"
                    )
                    # Reset number_to_use as the number was not acquired and must not be used
                    number_to_use = None
                    continue

                call_provider = get_voice_provider(
                    number_to_use.provider,
                    session,
                    config.telephony_config,
                )
                customer_mobile = (locked_lead.payload or {}).get(
                    "customer_mobile_number"
                )
                if not customer_mobile or not isinstance(customer_mobile, str):
                    logger.error(
                        f"Invalid customer_mobile_number for lead {locked_lead.id}"
                    )
                    continue
                call = call_provider.make_call(
                    customer_mobile,
                    number_to_use.number,
                    reseller_id=locked_lead.reseller_id,
                    template_name=locked_lead.template,
                )

                if call and call.get("sid"):
                    call_initiated = True  # Mark call as successfully initiated
                    await update_lead_call_details(
                        locked_lead.id,
                        LeadCallStatus.PROCESSING,
                        call.get("sid"),
                        datetime.now(timezone.utc),
                        number_to_use.id,
                    )
                else:
                    logger.error(
                        f"Failed to initiate call for lead {locked_lead.id}. Call response: {call}"
                    )
                    # Handle call initiation failure with retry logic
                    # Note: _attempt_fallback_call releases the number internally
                    await _attempt_fallback_call(
                        session,
                        locked_lead,
                        number_to_use,
                        config,
                        template,
                    )
                    number_acquired = False  # Number already released by fallback

            except Exception as e:
                logger.error(f"Error processing lead {lead.id}: {e}")
            finally:
                # Release acquired number only if call was NOT successfully initiated
                # (successful calls keep the number until handle_call_completion)
                if number_acquired and number_to_use and not call_initiated:
                    try:
                        await release_number(number_to_use.id, number_to_use.provider)
                    except Exception as release_err:
                        logger.error(f"Error releasing number: {release_err}")
                # Always release lead lock to prevent distributed lock exhaustion
                if locked_lead:
                    try:
                        await release_lock_on_lead_by_id(locked_lead.id)
                    except Exception as release_err:
                        logger.error(f"Error releasing lead lock: {release_err}")


# Alias for backward compatibility with existing imports
initiate_calls = process_backlog_leads

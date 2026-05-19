"""
Cron manager for handling background tasks.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.ai.voice.agents.breeze_buddy.managers.pre_checks import run_pre_checks
from app.ai.voice.agents.breeze_buddy.managers.utils import (
    prepare_and_store_initial_greeting,
)
from app.ai.voice.agents.breeze_buddy.services.agent_router.client import (
    safe_release_pod,
)
from app.ai.voice.agents.breeze_buddy.services.call_limiter import (
    check_outbound_rate_limit_and_alert,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.exotel.recording import (
    download_call_recording as download_call_recording_exotel,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.recording import (
    download_call_recording as download_call_recording_plivo,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.twilio.recording import (
    download_call_recording as download_call_recording_twilio,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.utils import get_voice_provider
from app.ai.voice.agents.breeze_buddy.template.types import (
    TemplateModel,
)
from app.ai.voice.agents.breeze_buddy.utils.common import send_webhook_with_retry
from app.ai.voice.agents.breeze_buddy.utils.playground import (
    apply_playground_config_overrides,
)
from app.core.config.static import (
    UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor import (
    acquire_lock_on_lead_by_id,
    create_lead_call_tracker,
    decrement_outbound_number_channels,
    defer_lead_next_attempt_and_release_lock,
    get_call_execution_config_by_merchant_id,
    get_lead_by_call_id,
    get_leads_based_on_status_and_next_attempt,
    get_leads_by_status_and_time_before,
    get_outbound_number_based_on_status_and_provider,
    get_outbound_number_by_id,
    get_template_by_id_with_fallback,
    increment_outbound_number_channels,
    is_number_blacklisted,
    release_lock_on_lead_by_id,
    update_lead_call_completion_details,
    update_lead_call_details,
    update_lead_call_recording_url,
    update_outbound_number_status,
)
from app.schemas import (
    CallDirection,
    CallExecutionConfig,
    CallProvider,
    ExecutionMode,
    LeadCallStatus,
    LeadCallTracker,
    OutboundNumber,
    OutboundNumberStatus,
)
from app.services.gcp.storage.storage import upload_file_to_gcs
from app.services.redis.client import get_redis_service


async def _get_lead_config(lead: LeadCallTracker) -> Optional[CallExecutionConfig]:
    """
    Retrieves the call execution configuration for a given lead.
    """

    configs = await get_call_execution_config_by_merchant_id(
        lead.reseller_id, lead.merchant_id
    )
    if not configs:
        logger.warning(
            f"No call execution config found for reseller: {lead.reseller_id} and shop: {lead.merchant_id}"
        )
        return None

    # Two-step: prefer exact template_id match; fall back to name only when
    # no template_id match exists or the config has no template_id of its own
    # (prevents accidentally picking a config with a conflicting template_id).
    config: Optional[CallExecutionConfig] = None
    if lead.template_id:
        config = next(
            (c for c in configs if c.template_id and c.template_id == lead.template_id),
            None,
        )
    if not config:
        config = next(
            (
                c
                for c in configs
                if c.template == lead.template
                and (not lead.template_id or not c.template_id)
            ),
            None,
        )
    if not config:
        # Step 3: fall back to the default config (template IS NULL)
        # Prefer merchant-specific default, then reseller-wide default
        if lead.merchant_id:
            config = next(
                (
                    c
                    for c in configs
                    if c.template is None and c.merchant_id == lead.merchant_id
                ),
                None,
            )
        if not config:
            config = next(
                (c for c in configs if c.template is None and c.merchant_id is None),
                None,
            )
    if not config:
        logger.warning(
            f"No call execution config found for template: {lead.template} (template_id={lead.template_id})"
        )
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


async def _run_pre_checks_for_lead(
    config: CallExecutionConfig,
    lead: LeadCallTracker,
    template: Optional[TemplateModel],
    session,
) -> bool:
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
    if reporting_webhook_url:
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


async def _get_available_number(
    config: CallExecutionConfig,
    template: Optional[TemplateModel],
) -> Optional[OutboundNumber]:
    """
    Finds an available outbound number for a given configuration.

    First tries the new approach (template with outbound_number_id).
    Falls back to backward compatible approach (matching by reseller/shop).
    """

    number = None

    if template and template.outbound_number_id:
        logger.info(
            f"Using new approach: template {config.template} has outbound_number_id {template.outbound_number_id}"
        )
        outbound_number = await get_outbound_number_by_id(template.outbound_number_id)

        if outbound_number and outbound_number.status == OutboundNumberStatus.AVAILABLE:
            if outbound_number.provider == CallProvider.EXOTEL:
                if (
                    outbound_number.channels is not None
                    and outbound_number.maximum_channels is not None
                    and outbound_number.channels < outbound_number.maximum_channels
                ):
                    number = outbound_number
            elif outbound_number.provider == CallProvider.TWILIO:
                number = outbound_number
            elif outbound_number.provider == CallProvider.PLIVO:
                number = outbound_number

    else:
        logger.info(
            f"Using backward compatible approach: looking for outbound number "
            f"matching reseller {config.reseller_id}, shop {config.merchant_id}"
        )

        # Get all available numbers
        all_available_numbers = await get_outbound_number_based_on_status_and_provider(
            OutboundNumberStatus.AVAILABLE, config.calling_provider
        )

        # Filter by reseller_id and merchant_id as none for fallback
        matching_numbers = [
            n
            for n in all_available_numbers
            if n.reseller_id is None and n.merchant_id is None
        ]

        if matching_numbers:
            for num in matching_numbers:
                if num.provider == CallProvider.EXOTEL:
                    if (
                        num.channels is not None
                        and num.maximum_channels is not None
                        and num.channels < num.maximum_channels
                    ):
                        number = num
                        break
                else:
                    number = num
                    break

    if not number:
        # Support both new and old field names for config
        config_reseller_id = config.reseller_id
        config_merchant_id = config.merchant_id
        logger.warning(
            f"No outbound number found for reseller {config_reseller_id}, "
            f"template {config.template}, shop {config_merchant_id}"
        )
        return None

    logger.info(
        f"Using outbound number {number.number} (provider: {number.provider}) "
        f"for template {config.template}, reseller {config.reseller_id}, shop {config.merchant_id}"
    )
    return number


async def _acquire_number(number: OutboundNumber) -> bool:
    """
    Marks an outbound number as in use.
    Uses atomic increment to avoid race conditions.
    For Exotel, only succeeds if channels < maximum_channels.
    Returns True if acquisition succeeded, False if at capacity.
    """
    if number.provider == CallProvider.TWILIO:
        result = await update_outbound_number_status(
            number.id, OutboundNumberStatus.IN_USE
        )
        return result is not None
    elif number.provider == CallProvider.EXOTEL:
        result = await increment_outbound_number_channels(number.id)
        return result is not None
    elif number.provider == CallProvider.PLIVO:
        result = await increment_outbound_number_channels(number.id)
        return result is not None
    return False


async def _release_number(number_id: str, provider: CallProvider):
    """
    Releases an outbound number, making it available for other calls.
    Uses atomic decrement to avoid race conditions.
    """
    if provider == CallProvider.TWILIO:
        await update_outbound_number_status(number_id, OutboundNumberStatus.AVAILABLE)
    elif provider == CallProvider.EXOTEL:
        await decrement_outbound_number_channels(number_id)
    elif provider == CallProvider.PLIVO:
        await decrement_outbound_number_channels(number_id)


async def _retry_call(
    lead: LeadCallTracker, config: CallExecutionConfig, outcome: Optional[str] = None
):
    """
    Schedules a retry for a call and sends webhook for NO_ANSWER outcomes.
    """
    is_last_attempt = lead.attempt_count >= config.max_retry - 1

    # Send webhook for NO_ANSWER on every attempt
    if outcome == "NO_ANSWER":
        reporting_webhook_url = (lead.payload or {}).get("reporting_webhook_url")
        if reporting_webhook_url:
            call_duration = None
            if lead.call_initiated_time:
                call_initiated_time_utc = lead.call_initiated_time.astimezone(
                    timezone.utc
                )
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
                async with create_aiohttp_session() as session:
                    success = await send_webhook_with_retry(
                        session, reporting_webhook_url, summary_data, max_retries=3
                    )
                    if success:
                        logger.info(
                            f"Successfully sent call summary webhook on no_answer (attempt {lead.attempt_count + 1}, isLastAttempt: {is_last_attempt})."
                        )
                    else:
                        logger.error(
                            "Failed to send call summary webhook on no_answer after all retries."
                        )
            except Exception as e:
                logger.error(f"Error sending webhook on no_answer: {e}")

    # Schedule retry if not the last attempt
    if not is_last_attempt:
        next_attempt_at = datetime.now(timezone.utc) + timedelta(
            seconds=config.retry_offset
        )
        await create_lead_call_tracker(
            id=str(uuid.uuid4()),
            reseller_id=lead.reseller_id,
            template=lead.template,
            template_id=lead.template_id,
            merchant_id=lead.merchant_id,
            next_attempt_at=next_attempt_at,
            payload=lead.payload,
            attempt_count=lead.attempt_count + 1,
            request_id=lead.request_id,
            meta_data={},
            call_direction=lead.call_direction,  # Inherit call direction from parent lead
        )


async def _cleanup_stuck_leads():
    """
    Cleans up leads that are stuck in the PROCESSING state.
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
            # Atomically acquire lock AND verify still in PROCESSING (single DB trip)
            locked_lead = await acquire_lock_on_lead_by_id(
                lead.id, expected_status=LeadCallStatus.PROCESSING
            )
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
                    await _release_number(outbound_number.id, outbound_number.provider)

            # Only retry outbound telephony calls - inbound and test calls should not be retried
            config = await _get_lead_config(locked_lead)
            if (
                config
                and locked_lead.call_direction == CallDirection.OUTBOUND
                and locked_lead.execution_mode == ExecutionMode.TELEPHONY
            ):
                await _retry_call(locked_lead, config)

        except Exception as e:
            logger.error(f"Error cleaning up stuck lead {lead.id}: {e}")

        finally:
            if locked_lead:
                await release_lock_on_lead_by_id(locked_lead.id)


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
            try:
                # Atomically acquire lock AND verify status is still BACKLOG in one DB trip.
                # Multiple concurrent process_backlog_leads invocations hold stale snapshots
                # of BACKLOG leads — by the time we reach this lead, another invocation may
                # have already processed it. The expected_status guard prevents locking a
                # lead that's already in PROCESSING or FINISHED.
                locked_lead = await acquire_lock_on_lead_by_id(
                    lead.id, expected_status=LeadCallStatus.BACKLOG
                )
                if not locked_lead:
                    continue

                # Now we have exclusive access to this BACKLOG lead
                logger.info(f"Successfully locked lead {lead.id} for processing.")

                config = await _get_lead_config(locked_lead)
                if not config:
                    await release_lock_on_lead_by_id(locked_lead.id)
                    continue
                # Support both new and old field names for lead
                if not config.enable_calling:
                    logger.info(
                        f"Skipping lead {locked_lead.id} - calling is disabled for reseller {locked_lead.reseller_id}, template {locked_lead.template}"
                    )
                    await release_lock_on_lead_by_id(locked_lead.id)
                    continue

                customer_phone = (locked_lead.payload or {}).get(
                    "customer_mobile_number"
                )

                if customer_phone and await is_number_blacklisted(
                    customer_phone, locked_lead.reseller_id
                ):
                    masked_phone = (
                        f"***{customer_phone[-4:]}"
                        if len(customer_phone) >= 4
                        else "***"
                    )
                    logger.info(
                        f"Skipping lead {locked_lead.id} - phone number {masked_phone} is blacklisted"
                    )
                    await update_lead_call_completion_details(
                        id=locked_lead.id,
                        status=LeadCallStatus.FINISHED,
                        outcome="BLACKLISTED",
                        meta_data={"reason": "Phone number is blacklisted"},
                        call_end_time=datetime.now(timezone.utc),
                    )
                    await release_lock_on_lead_by_id(locked_lead.id)
                    continue

                if not _is_within_calling_hours(config):
                    logger.info(
                        f"Skipping lead {locked_lead.id} - outside calling hours. "
                        f"Current time: {datetime.now(timezone(timedelta(hours=5, minutes=30))).time()}, "
                        f"Allowed window: {config.call_start_time} - {config.call_end_time}"
                    )
                    await release_lock_on_lead_by_id(locked_lead.id)
                    continue
                template = await get_template_by_id_with_fallback(
                    template_id=locked_lead.template_id,
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
                    await release_lock_on_lead_by_id(locked_lead.id)
                    continue

                # Synthesize initial greeting audio and store in Redis.
                # Apply playground overrides first — they are normally applied
                # later in flow.py (too late for greeting synthesis).
                if template:
                    apply_playground_config_overrides(locked_lead, template)
                    await prepare_and_store_initial_greeting(
                        lead_id=locked_lead.id,
                        payload=locked_lead.payload or {},
                        template=template,
                    )

                number_to_use = await _get_available_number(config, template)
                if not number_to_use:
                    await release_lock_on_lead_by_id(locked_lead.id)
                    continue

                acquired = await _acquire_number(number_to_use)
                if not acquired:
                    logger.warning(
                        f"Failed to acquire number {number_to_use.id} for lead {locked_lead.id} - "
                        "number may be at maximum capacity"
                    )
                    # Reset number_to_use as the number was not acquired and must not be used
                    number_to_use = None
                    await release_lock_on_lead_by_id(locked_lead.id)
                    continue

                call_provider = get_voice_provider(
                    number_to_use.provider,
                    session,
                    config.telephony_config,
                )

                # Pod allocation now happens at webhook time (when customer answers)
                # via provider-specific answer webhooks. No pre-allocation needed.
                # merchant_id is passed for tiered pod allocation.

                customer_mobile = (locked_lead.payload or {}).get(
                    "customer_mobile_number"
                )
                if not customer_mobile or not isinstance(customer_mobile, str):
                    logger.error(
                        f"Invalid customer_mobile_number for lead {locked_lead.id}"
                    )
                    await _release_number(number_to_use.id, number_to_use.provider)
                    await release_lock_on_lead_by_id(locked_lead.id)
                    continue
                if locked_lead.execution_mode == ExecutionMode.TELEPHONY:
                    rate_limit_allowed, defer_seconds = (
                        await check_outbound_rate_limit_and_alert(
                            customer_phone=customer_mobile,
                            lead_id=str(locked_lead.id),
                            reseller_id=locked_lead.reseller_id,
                        )
                    )
                    if not rate_limit_allowed:
                        # Push next_attempt_at out by the rate-limit window so the
                        # cron does not immediately re-pick this lead and re-trigger
                        # the alert on the next cycle.
                        await _release_number(number_to_use.id, number_to_use.provider)
                        deferred = await defer_lead_next_attempt_and_release_lock(
                            locked_lead.id, defer_seconds
                        )
                        if deferred is None:
                            # Defer accessor failed; fall back to a plain unlock so
                            # the lead does not stay stuck in locked state.
                            logger.error(
                                f"Failed to defer next_attempt_at for lead "
                                f"{locked_lead.id}; releasing lock without deferral"
                            )
                            await release_lock_on_lead_by_id(locked_lead.id)
                        continue
                call = call_provider.make_call(
                    customer_mobile,
                    number_to_use.number,
                    reseller_id=locked_lead.reseller_id,
                    template_name=locked_lead.template,
                )

                if call and call.get("sid"):
                    actual_call_sid = str(call.get("sid"))

                    updated = await update_lead_call_details(
                        locked_lead.id,
                        LeadCallStatus.PROCESSING,
                        actual_call_sid,
                        datetime.now(timezone.utc),
                        number_to_use.id,
                    )
                    if not updated:
                        # Another invocation already moved this lead out of BACKLOG.
                        # The call was placed but the lead is no longer ours — release resources.
                        logger.warning(
                            f"Lead {locked_lead.id} was already processed by another invocation "
                            f"(status changed from BACKLOG). Releasing number. "
                            f"Call {actual_call_sid} may be orphaned."
                        )
                        await _release_number(number_to_use.id, number_to_use.provider)
                        try:
                            redis = await get_redis_service()
                            await redis.delete(f"greeting:{locked_lead.id}")
                        except Exception:
                            pass
                        await release_lock_on_lead_by_id(locked_lead.id)
                        continue
                else:
                    logger.error(
                        f"Failed to initiate call for lead {locked_lead.id}. Call response: {call}"
                    )
                    await _release_number(number_to_use.id, number_to_use.provider)

                    if template and template.outbound_number_id:
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

                        # Send webhook for failed call
                        reporting_webhook_url = (
                            locked_lead.payload.get("reporting_webhook_url")
                            if locked_lead.payload
                            else None
                        )
                        if reporting_webhook_url:
                            webhook_data = {
                                "outcome": "FAILED",
                                "attemptCount": locked_lead.attempt_count + 1,
                                "failureReason": "Failed to initiate call, no retry due to fixed outbound number.",
                                "orderId": locked_lead.request_id,
                            }
                            logger.info(
                                f"Sending failure webhook for lead {locked_lead.id} to {reporting_webhook_url}"
                            )
                            try:
                                await send_webhook_with_retry(
                                    session, reporting_webhook_url, webhook_data
                                )
                            except Exception as e:
                                logger.error(
                                    f"Error sending failure webhook for lead {locked_lead.id}: {e}"
                                )

                        await release_lock_on_lead_by_id(locked_lead.id)

                        continue

                    retry_calling_provider = None

                    if number_to_use.provider == CallProvider.TWILIO:
                        retry_calling_provider = CallProvider.EXOTEL
                    elif number_to_use.provider == CallProvider.EXOTEL:
                        if not config.enable_international_call:
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

                            # Send webhook for failed call
                            reporting_webhook_url = (
                                locked_lead.payload.get("reporting_webhook_url")
                                if locked_lead.payload
                                else None
                            )
                            if reporting_webhook_url:
                                webhook_data = {
                                    "outcome": "FAILED",
                                    "attemptCount": locked_lead.attempt_count + 1,
                                    "failureReason": "Failed to initiate call due to NCPR, international calling disabled.",
                                    "orderId": locked_lead.request_id,
                                }
                                logger.info(
                                    f"Sending failure webhook for lead {locked_lead.id} to {reporting_webhook_url}"
                                )
                                try:
                                    await send_webhook_with_retry(
                                        session, reporting_webhook_url, webhook_data
                                    )
                                except Exception as e:
                                    logger.error(
                                        f"Error sending failure webhook for lead {locked_lead.id}: {e}"
                                    )

                            await release_lock_on_lead_by_id(locked_lead.id)

                            continue
                        retry_calling_provider = CallProvider.TWILIO

                    if not retry_calling_provider:
                        # No retry provider available, skip retry
                        await release_lock_on_lead_by_id(locked_lead.id)
                        continue

                    retry_number_to_use = None

                    # First, get all available numbers with the retry provider
                    retry_numbers = (
                        await get_outbound_number_based_on_status_and_provider(
                            OutboundNumberStatus.AVAILABLE, retry_calling_provider
                        )
                    )

                    if retry_numbers:
                        for number in retry_numbers:
                            if (
                                number.reseller_id is None
                                and number.merchant_id is None
                            ):
                                if retry_calling_provider == CallProvider.EXOTEL:
                                    if (
                                        number.channels is not None
                                        and number.maximum_channels is not None
                                        and number.channels < number.maximum_channels
                                    ):
                                        retry_number_to_use = number
                                        break
                                else:
                                    retry_number_to_use = number
                                    break

                    if not retry_number_to_use:
                        await release_lock_on_lead_by_id(locked_lead.id)
                        continue

                    retry_acquired = await _acquire_number(retry_number_to_use)
                    if not retry_acquired:
                        logger.warning(
                            f"Failed to acquire retry number {retry_number_to_use.id} for lead {locked_lead.id} - "
                            "number may be at maximum capacity"
                        )
                        retry_number_to_use = None
                        await release_lock_on_lead_by_id(locked_lead.id)
                        continue

                    retry_call_provider = get_voice_provider(
                        retry_calling_provider,
                        session,
                        config.telephony_config,
                    )

                    # Pod allocation happens at webhook time (when customer answers)
                    # via provider-specific answer webhooks.
                    retry_customer_mobile = (locked_lead.payload or {}).get(
                        "customer_mobile_number"
                    )
                    if not retry_customer_mobile or not isinstance(
                        retry_customer_mobile, str
                    ):
                        logger.error(
                            f"Invalid customer_mobile_number for retry lead {locked_lead.id}"
                        )
                        await _release_number(
                            retry_number_to_use.id, retry_number_to_use.provider
                        )
                        await release_lock_on_lead_by_id(locked_lead.id)
                        continue
                    retry_call = retry_call_provider.make_call(
                        retry_customer_mobile,
                        retry_number_to_use.number,
                        reseller_id=locked_lead.reseller_id,
                        template_name=locked_lead.template,
                    )

                    if retry_call and retry_call.get("sid"):
                        retry_call_sid = str(retry_call.get("sid"))
                        retry_updated = await update_lead_call_details(
                            locked_lead.id,
                            LeadCallStatus.PROCESSING,
                            retry_call_sid,
                            datetime.now(timezone.utc),
                            retry_number_to_use.id,
                        )
                        if not retry_updated:
                            logger.warning(
                                f"Lead {locked_lead.id} was already processed by another invocation "
                                f"during retry call. Releasing retry number. "
                                f"Call {retry_call_sid} may be orphaned."
                            )
                            await _release_number(
                                retry_number_to_use.id, retry_number_to_use.provider
                            )
                            try:
                                redis = await get_redis_service()
                                await redis.delete(f"greeting:{locked_lead.id}")
                            except Exception:
                                pass
                            await release_lock_on_lead_by_id(locked_lead.id)
                            continue
                    else:
                        logger.error(
                            f"Failed to initiate retry call for lead {locked_lead.id}. Call response: {retry_call}"
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
                        await _release_number(
                            retry_number_to_use.id, retry_number_to_use.provider
                        )

                        # Send webhook for failed call
                        reporting_webhook_url = (
                            locked_lead.payload.get("reporting_webhook_url")
                            if locked_lead.payload
                            else None
                        )
                        if reporting_webhook_url:
                            webhook_data = {
                                "outcome": "FAILED",
                                "attemptCount": locked_lead.attempt_count + 1,
                                "failureReason": "Failed to initiate call with both providers.",
                                "orderId": locked_lead.request_id,
                            }
                            logger.info(
                                f"Sending failure webhook for lead {locked_lead.id} to {reporting_webhook_url}"
                            )
                            try:
                                await send_webhook_with_retry(
                                    session, reporting_webhook_url, webhook_data
                                )
                            except Exception as e:
                                logger.error(
                                    f"Error sending failure webhook for lead {locked_lead.id}: {e}"
                                )

                await release_lock_on_lead_by_id(locked_lead.id)

            except Exception as e:
                logger.error(f"Error processing lead {lead.id}: {e}")


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

    # Release pod early — even if the lead lookup fails (orphaned call_id),
    # the pod must be freed to avoid pod leaks when pod isolation is enabled.
    await safe_release_pod(call_sid=call_id, reason="call_completed")

    lead = await get_lead_by_call_id(call_id)
    if not lead:
        logger.error(
            f"Could not find lead for call_id: {call_id}. "
            f"Outbound number channel may be leaked — manual cleanup required. "
            f"This can happen when a prior duplicate-call bug overwrote the call_id on the lead."
        )
        return

    # Always release outbound number (including transfers — bot leaves, cleanup happens here)
    if lead.outbound_number_id:
        outbound_number = await get_outbound_number_by_id(lead.outbound_number_id)
        if outbound_number:
            await _release_number(outbound_number.id, outbound_number.provider)
        else:
            logger.error(
                f"Could not find outbound number with id: {lead.outbound_number_id} to release."
            )
    else:
        logger.info(f"No outbound number id for lead: {lead.id}")

    # Check if this is a transfer — for outcome override only
    is_transfer = (
        meta_data
        and "transfer" in meta_data
        and meta_data.get("transfer", {}).get("status") == "success"
    ) or (
        lead.metaData and lead.metaData.get("transfer", {}).get("status") == "success"
    )

    config = await _get_lead_config(lead)
    if not config:
        return

    # Override outcome to "TRANSFERRED" for transfer calls
    if is_transfer:
        outcome = "TRANSFERRED"

    updated_lead = await update_lead_call_completion_details(
        id=lead.id,
        status=LeadCallStatus.FINISHED,
        outcome=outcome,
        meta_data=meta_data,
        call_end_time=call_end_time,
    )

    # Only retry outbound telephony calls - inbound and test calls should not be retried
    if (
        outcome in ["BUSY", "NO_ANSWER"]
        and lead.call_direction == CallDirection.OUTBOUND
        and lead.execution_mode == ExecutionMode.TELEPHONY
    ):
        await _retry_call(lead, config, outcome)

    return updated_lead


async def handle_unanswered_calls(call_id: str):
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
        logger.error(
            f"Could not find lead for call_id: {call_id}. "
            f"Outbound number channel may be leaked — manual cleanup required. "
            f"This can happen when a prior duplicate-call bug overwrote the call_id on the lead."
        )
        return

    # Clean up greeting audio from Redis if it exists — do this regardless of status
    # since the delete is idempotent and prevents Redis key leaks from orphaned calls.
    try:
        redis = await get_redis_service()
        greeting_key = f"greeting:{lead.id}"
        await redis.delete(greeting_key)
        logger.info(f"Deleted greeting audio from Redis for lead {lead.id}")
    except Exception as e:
        logger.warning(
            f"Failed to delete greeting audio from Redis for lead {lead.id}: {e}"
        )

    # Release outbound number channel — do this before the FINISHED guard because the
    # channel must be freed regardless of lead status. _release_number is idempotent
    # (SQL uses GREATEST(0, ...)), so duplicate releases are safe.
    if lead.outbound_number_id:
        outbound_number = await get_outbound_number_by_id(lead.outbound_number_id)
        if outbound_number:
            await _release_number(outbound_number.id, outbound_number.provider)
        else:
            logger.error(
                f"Could not find outbound number with id: {lead.outbound_number_id} to release."
            )
    else:
        logger.info(f"No outbound number id for lead: {lead.id}")

    # Guard: if another callback already finished this lead, skip to avoid duplicate retries.
    # This happens when the lock race causes multiple calls for the same lead — each call's
    # callback arrives independently and would otherwise each create a retry entry.
    if lead.status == LeadCallStatus.FINISHED:
        logger.info(
            f"Lead {lead.id} is already FINISHED for call_id: {call_id}, skipping unanswered handler."
        )
        return

    config = await _get_lead_config(lead)
    if not config:
        return

    await update_lead_call_completion_details(
        id=lead.id,
        status=LeadCallStatus.FINISHED,
        outcome="NO_ANSWER",
        meta_data={},
        call_end_time=datetime.now(timezone.utc),
    )

    # Only retry outbound telephony calls - inbound and test calls should not be retried
    if (
        lead.call_direction == CallDirection.OUTBOUND
        and lead.execution_mode == ExecutionMode.TELEPHONY
    ):
        await _retry_call(lead, config, "NO_ANSWER")


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
    logger.info(
        f"Processing call recording for call_id: {call_id} from provider: {provider}"
    )
    lead = await get_lead_by_call_id(call_id)
    provider = provider.lower()
    if not lead:
        logger.error(f"Could not find lead for call_id: {call_id}")
        return

    try:
        # If cloud upload is disabled, just store the provider URL in DB
        if not UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD:
            logger.info(
                f"Cloud upload disabled. Storing provider recording URL in DB for call_id: {call_id}"
            )
            await update_lead_call_recording_url(call_id, provider_recording_url)
            return

        # Cloud upload is enabled - download from provider and upload to GCS
        if provider == "twilio":
            audio_file = await download_call_recording_twilio(
                provider_recording_url, call_id
            )
        elif provider == "exotel":
            audio_file = await download_call_recording_exotel(
                provider_recording_url, call_id
            )
        elif provider == "plivo":
            audio_file = await download_call_recording_plivo(
                provider_recording_url, call_id
            )
        else:
            logger.error(f"Unsupported provider: {provider}")
            return

        if not audio_file:
            logger.error(f"Failed to download recording for call_id: {call_id}")
            await update_lead_call_recording_url(call_id, provider_recording_url)
            return

        if provider == "twilio":
            content_type = "audio/wav"
            file_extension = "wav"
        elif provider == "plivo":
            content_type = "audio/mpeg"
            file_extension = "mp3"
        else:  # exotel
            content_type = "audio/mp3"
            file_extension = "mp3"

        gcs_url = upload_file_to_gcs(
            file_obj=audio_file,
            destination_path=f"breeze-buddy/recordings/{call_id}.{file_extension}",
            content_type=content_type,
            metadata={
                "call_id": call_id,
                "original_url": provider_recording_url,
            },
        )

        if gcs_url:
            logger.info(f"Successfully uploaded recording to GCS: {gcs_url}")
            await update_lead_call_recording_url(call_id, gcs_url)
        else:
            logger.error(f"Failed to upload recording to GCS for call_id: {call_id}")
            await update_lead_call_recording_url(call_id, provider_recording_url)

    except Exception as e:
        logger.error(
            f"Error processing call recording for call_id {call_id}: {e}", exc_info=True
        )

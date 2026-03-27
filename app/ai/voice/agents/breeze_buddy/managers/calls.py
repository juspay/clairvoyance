"""
Call lifecycle manager — handles lead processing, call initiation, and completion.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

from app.ai.voice.agents.breeze_buddy.managers.lead_dispatcher import (
    get_lead_dispatcher,
)
from app.ai.voice.agents.breeze_buddy.managers.pre_checks import run_pre_checks
from app.ai.voice.agents.breeze_buddy.managers.resource_manager import (
    CallResourceManager,
    _acquire_number,
    _release_number,
)
from app.ai.voice.agents.breeze_buddy.services.agent_router.client import (
    safe_release_pod,
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
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.utils.common import send_webhook_with_retry
from app.core.config.static import UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor import (
    acquire_lock_on_lead_by_id,
    create_lead_call_tracker,
    get_call_execution_config_by_merchant_id,
    get_lead_by_call_id,
    get_leads_by_status_and_time_before,
    get_outbound_number_based_on_status_and_provider,
    get_outbound_number_by_id,
    get_template_by_merchant,
    is_number_blacklisted,
    release_lock_on_lead_by_id,
    update_lead_call_completion_details,
    update_lead_call_details,
    update_lead_call_recording_url,
    update_lead_next_attempt_at,
)
from app.schemas import (
    CallDirection,
    CallExecutionConfig,
    CallProvider,
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

    config = next((c for c in configs if c.template == lead.template), None)
    if not config:
        logger.warning(f"No call execution config found for template: {lead.template}")
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
        new_lead_id = str(uuid.uuid4())
        await create_lead_call_tracker(
            id=new_lead_id,
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
        # Notify dispatcher about the new retry lead
        dispatcher = get_lead_dispatcher()
        if dispatcher:
            await dispatcher.on_lead_created(new_lead_id, next_attempt_at)


async def cleanup_stuck_leads():
    """
    Cleans up leads that are stuck in the PROCESSING state for >10 minutes.
    Releases all resources (pod, number, greeting) and optionally retries.
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
            locked_lead = await acquire_lock_on_lead_by_id(
                lead.id, expected_status=LeadCallStatus.PROCESSING
            )
            if not locked_lead:
                continue

            logger.info(f"Cleaning up stuck lead {locked_lead.id}")

            # Release pod if call_id exists (idempotent)
            if locked_lead.call_id:
                await safe_release_pod(
                    call_sid=locked_lead.call_id, reason="stuck_timeout"
                )

            # Release outbound number
            if locked_lead.outbound_number_id:
                outbound_number = await get_outbound_number_by_id(
                    locked_lead.outbound_number_id
                )
                if outbound_number:
                    await _release_number(outbound_number.id, outbound_number.provider)

            # Delete greeting from Redis (idempotent)
            try:
                redis = await get_redis_service()
                await redis.delete(f"greeting:{locked_lead.id}")
            except Exception:
                pass

            # Mark FINISHED
            await update_lead_call_completion_details(
                id=locked_lead.id,
                status=LeadCallStatus.FINISHED,
                outcome="UNKNOWN",
                meta_data={"cleanup": "stuck_processing_timeout"},
                call_end_time=datetime.now(timezone.utc),
            )

            # Retry outbound calls
            config = await _get_lead_config(locked_lead)
            if config and locked_lead.call_direction == CallDirection.OUTBOUND:
                await _retry_call(locked_lead, config)

            # Signal channel freed
            dispatcher = get_lead_dispatcher()
            if dispatcher:
                await dispatcher.on_channel_freed()

        except Exception as e:
            logger.error(f"Error cleaning up stuck lead {lead.id}: {e}")

        finally:
            if locked_lead:
                await release_lock_on_lead_by_id(locked_lead.id)


async def process_single_lead(lead_id: str, session: aiohttp.ClientSession) -> None:
    """Process a single BACKLOG lead end-to-end.

    Grabs the lead atomically (SKIP LOCKED), validates, acquires resources,
    makes the call, and cleans up on any failure via CallResourceManager.

    Called by LeadDispatcher workers. The lead may already be locked by
    grab_next_backlog_lead (on_channel_freed path) or may need locking
    (on_lead_created / startup recovery path).
    """
    # Try to lock the lead. If already locked by grab_next_backlog_lead,
    # this will return the already-locked lead. If it was enqueued by ID
    # (from on_lead_created), we need to lock it now.
    locked_lead = await acquire_lock_on_lead_by_id(
        lead_id, expected_status=LeadCallStatus.BACKLOG
    )
    if not locked_lead:
        logger.debug(f"Lead {lead_id} already processed or locked, skipping")
        return

    try:
        async with CallResourceManager(locked_lead) as resources:
            # --- Validate ---
            config = await _get_lead_config(locked_lead)
            if not config or not config.enable_calling:
                logger.info(
                    f"Lead {locked_lead.id}: config missing or calling disabled, skipping"
                )
                return

            blacklist_phone = (locked_lead.payload or {}).get("customer_mobile_number")
            if blacklist_phone and await is_number_blacklisted(
                blacklist_phone, locked_lead.reseller_id
            ):
                logger.info(
                    f"Lead {locked_lead.id}: phone {blacklist_phone} is blacklisted"
                )
                await update_lead_call_completion_details(
                    id=locked_lead.id,
                    status=LeadCallStatus.FINISHED,
                    outcome="BLACKLISTED",
                    meta_data={"reason": "Phone number is blacklisted"},
                    call_end_time=datetime.now(timezone.utc),
                )
                return

            if not _is_within_calling_hours(config):
                logger.info(
                    f"Lead {locked_lead.id}: outside calling hours, rescheduling"
                )
                # Re-schedule this lead for the start of next calling window.
                # Without this, the lead sits in BACKLOG with no event to wake it.
                IST = timezone(timedelta(hours=5, minutes=30))
                now_ist = datetime.now(IST)
                # Schedule for start of next calling window (today or tomorrow)
                start = datetime.combine(
                    now_ist.date(), config.call_start_time, tzinfo=IST
                )
                if start <= now_ist:
                    start += timedelta(days=1)
                next_window_utc = start.astimezone(timezone.utc)
                # Persist to DB so the lead survives Redis flush
                await update_lead_next_attempt_at(locked_lead.id, next_window_utc)
                dispatcher = get_lead_dispatcher()
                if dispatcher:
                    await dispatcher.on_lead_created(locked_lead.id, next_window_utc)
                return

            template = await get_template_by_merchant(
                reseller_id=config.reseller_id,
                merchant_id=config.merchant_id,
                name=config.template,
            )

            if not await _run_pre_checks_for_lead(
                config, locked_lead, template, session
            ):
                return  # already marked FINISHED

            # --- Acquire Resources ---
            await resources.store_greeting(locked_lead.payload or {}, template)

            number = await resources.acquire_number(config, template)
            if not number:
                logger.info(f"Lead {locked_lead.id}: no outbound number available")
                return

            # --- Make Call ---
            call_provider = get_voice_provider(
                number.provider, session, config.telephony_config
            )
            customer_mobile = (locked_lead.payload or {}).get("customer_mobile_number")
            if not customer_mobile or not isinstance(customer_mobile, str):
                logger.error(
                    f"Invalid customer_mobile_number for lead {locked_lead.id}"
                )
                await update_lead_call_completion_details(
                    id=locked_lead.id,
                    status=LeadCallStatus.FINISHED,
                    outcome="INVALID_PAYLOAD",
                    meta_data={"reason": "Missing or invalid customer_mobile_number"},
                    call_end_time=datetime.now(timezone.utc),
                )
                return  # __aexit__ cleans up resources

            call = call_provider.make_call(
                customer_mobile,
                number.number,
                reseller_id=locked_lead.reseller_id,
                template_name=locked_lead.template,
            )

            if call and call.get("sid"):
                actual_call_sid = str(call.get("sid"))
                logger.info(
                    f"Lead {locked_lead.id}: primary call initiated, sid={actual_call_sid}, "
                    f"provider={number.provider.value}, number={number.number}"
                )
                updated = await update_lead_call_details(
                    locked_lead.id,
                    LeadCallStatus.PROCESSING,
                    actual_call_sid,
                    datetime.now(timezone.utc),
                    number.id,
                )
                if not updated:
                    # Another process moved this lead — cleanup
                    await resources.cleanup()
                    return
                # Success! Transfer resource ownership to callback handler
                resources.transfer_ownership()
                return

            # --- Primary call failed: try alternate provider ---
            logger.warning(
                f"Lead {locked_lead.id}: primary call failed with provider={number.provider.value}"
            )
            # Release number only — keep greeting for retry (same lead_id, same greeting)
            await resources.release_number()

            if template and template.outbound_number_id:
                # Fixed number — no retry with alternate provider
                await update_lead_call_completion_details(
                    id=locked_lead.id,
                    status=LeadCallStatus.FINISHED,
                    outcome="UNKNOWN",
                    meta_data={
                        "failure_reason": "Failed to initiate call, no retry due to fixed outbound number."
                    },
                    call_end_time=datetime.now(timezone.utc),
                )
                _send_failure_webhook_bg(
                    session,
                    locked_lead,
                    "Failed to initiate call, no retry due to fixed outbound number.",
                )
                return

            retry_provider = _get_retry_provider(number.provider)
            if not retry_provider:
                logger.info(
                    f"Lead {locked_lead.id}: no retry provider available for {number.provider.value}"
                )
                return

            if (
                number.provider == CallProvider.EXOTEL
                and not config.enable_international_call
            ):
                await update_lead_call_completion_details(
                    id=locked_lead.id,
                    status=LeadCallStatus.FINISHED,
                    outcome="UNKNOWN",
                    meta_data={
                        "failure_reason": "Failed to initiate call with EXOTEL, international calling disabled."
                    },
                    call_end_time=datetime.now(timezone.utc),
                )
                _send_failure_webhook_bg(
                    session,
                    locked_lead,
                    "Failed to initiate call due to NCPR, international calling disabled.",
                )
                return

            # Find and acquire retry number
            retry_number = await _find_retry_number(retry_provider)
            if not retry_number:
                logger.info(
                    f"Lead {locked_lead.id}: no retry number available for {retry_provider.value}"
                )
                return

            retry_acquired = await _acquire_number(retry_number)
            if not retry_acquired:
                logger.info(
                    f"Lead {locked_lead.id}: failed to acquire retry number {retry_number.id}"
                )
                return

            retry_number_owned = True
            try:
                retry_call_provider = get_voice_provider(
                    retry_provider, session, config.telephony_config
                )
                retry_call = retry_call_provider.make_call(
                    customer_mobile,
                    retry_number.number,
                    reseller_id=locked_lead.reseller_id,
                    template_name=locked_lead.template,
                )

                if retry_call and retry_call.get("sid"):
                    retry_sid = str(retry_call.get("sid"))
                    logger.info(
                        f"Lead {locked_lead.id}: retry call initiated, sid={retry_sid}, "
                        f"provider={retry_provider.value}, number={retry_number.number}"
                    )
                    retry_updated = await update_lead_call_details(
                        locked_lead.id,
                        LeadCallStatus.PROCESSING,
                        retry_sid,
                        datetime.now(timezone.utc),
                        retry_number.id,
                    )
                    if retry_updated:
                        retry_number_owned = False  # callback handler owns it now
                        return

                    # Another process moved this lead — we still own the number
                    return

                # Retry also failed
                await update_lead_call_completion_details(
                    id=locked_lead.id,
                    status=LeadCallStatus.FINISHED,
                    outcome="UNKNOWN",
                    meta_data={
                        "failure_reason": f"Failed to initiate call using {retry_provider.value} after {config.calling_provider.value} failed."
                    },
                    call_end_time=datetime.now(timezone.utc),
                )
                _send_failure_webhook_bg(
                    session, locked_lead, "Failed to initiate call with both providers."
                )
            finally:
                if retry_number_owned:
                    await _release_number(retry_number.id, retry_number.provider)

    finally:
        await release_lock_on_lead_by_id(lead_id)


def _get_retry_provider(primary_provider: CallProvider) -> Optional[CallProvider]:
    """Determine which provider to retry with after primary fails."""
    if primary_provider == CallProvider.TWILIO:
        return CallProvider.EXOTEL
    elif primary_provider == CallProvider.EXOTEL:
        return CallProvider.TWILIO
    return None


async def _find_retry_number(provider: CallProvider) -> Optional[OutboundNumber]:
    """Find an available number for the retry provider."""
    numbers = await get_outbound_number_based_on_status_and_provider(
        OutboundNumberStatus.AVAILABLE, provider
    )
    if numbers:
        for number in numbers:
            if number.reseller_id is None and number.merchant_id is None:
                if provider in (CallProvider.EXOTEL, CallProvider.PLIVO):
                    if (
                        number.channels is not None
                        and number.maximum_channels is not None
                        and number.channels < number.maximum_channels
                    ):
                        return number
                else:
                    return number
    return None


def _send_failure_webhook_bg(session, lead: LeadCallTracker, reason: str) -> None:
    """Fire-and-forget failure webhook (best effort)."""
    url = (lead.payload or {}).get("reporting_webhook_url")
    if not url:
        return
    data = {
        "outcome": "FAILED",
        "attemptCount": lead.attempt_count + 1,
        "failureReason": reason,
        "orderId": lead.request_id,
    }
    asyncio.create_task(
        send_webhook_with_retry(session, url, data),
        name=f"webhook-{lead.id}",
    )


async def handle_call_ended(
    call_id: str,
    outcome: str | None = None,
    call_end_time: datetime | None = None,
    meta_data: dict | None = None,
    call_status: str = "completed",
) -> Optional[LeadCallTracker]:
    """Unified handler for ALL call termination paths.

    Called from:
      - WebSocket close (provider completion callback): outcome set by agent
      - Status callback (no-answer/busy/failed/completed): call_status from provider
      - Stuck lead cleanup: outcome="UNKNOWN", call_status="stuck_timeout"

    Handles the complete resource release lifecycle in one place:
      1. Release pod (idempotent)
      2. Find lead
      3. FINISHED guard (race protection)
      4. Release outbound number
      5. Delete greeting from Redis
      6. Determine final outcome
      7. Update lead → FINISHED
      8. Retry if needed
      9. Signal on_channel_freed

    Args:
        call_id: Provider call SID (or lead ID for Daily mode)
        outcome: Explicit outcome from agent (e.g., "COMPLETED", "TRANSFERRED").
                 If None, derived from call_status.
        call_end_time: When the call ended (defaults to now)
        meta_data: Call metadata from agent (transcription, transfer info, etc.)
        call_status: Raw provider status (e.g., "no-answer", "busy", "completed").
                     Used for pod release reason and outcome fallback.
    """
    logger.info(
        f"Call ended: call_id={call_id}, outcome={outcome}, status={call_status}"
    )

    # --- 1. Release pod (idempotent, always safe to call) ---
    await safe_release_pod(call_sid=call_id, reason=call_status)

    # --- 2. Find lead ---
    lead = await get_lead_by_call_id(call_id)
    if not lead:
        logger.error(
            f"Could not find lead for call_id: {call_id}. "
            "Channel will be corrected by reconciliation."
        )
        dispatcher = get_lead_dispatcher()
        if dispatcher:
            await dispatcher.on_channel_freed()
        return None

    # --- 3. Release resources BEFORE the FINISHED guard ---
    # These must happen regardless of lead status to prevent channel/greeting leaks.
    # In a race (WebSocket close + status callback), both handlers release resources,
    # but only the first one proceeds past the FINISHED guard to update state/retry.
    if lead.outbound_number_id:
        outbound_number = await get_outbound_number_by_id(lead.outbound_number_id)
        if outbound_number:
            await _release_number(outbound_number.id, outbound_number.provider)
        else:
            logger.error(
                f"Could not find outbound number {lead.outbound_number_id} to release."
            )

    try:
        redis = await get_redis_service()
        await redis.delete(f"greeting:{lead.id}")
    except Exception:
        pass  # TTL will clean it up

    # --- 4. FINISHED guard (prevents duplicate retries and status updates) ---
    if lead.status == LeadCallStatus.FINISHED:
        logger.info(
            f"Lead {lead.id} already FINISHED for call_id: {call_id}, skipping."
        )
        dispatcher = get_lead_dispatcher()
        if dispatcher:
            await dispatcher.on_channel_freed()
        return None

    # --- 5. Determine final outcome ---
    if not outcome:
        # Map provider status to outcome: "no-answer" → "NO_ANSWER", "busy" → "BUSY"
        outcome = call_status.upper().replace("-", "_")

    # Transfer override: if transfer metadata indicates success, override outcome
    is_transfer = (
        meta_data and meta_data.get("transfer", {}).get("status") == "success"
    ) or (
        lead.metaData and lead.metaData.get("transfer", {}).get("status") == "success"
    )
    if is_transfer:
        outcome = "TRANSFERRED"

    # --- 7. Update lead → FINISHED (atomic: only succeeds if not already FINISHED) ---
    updated_lead = await update_lead_call_completion_details(
        id=lead.id,
        status=LeadCallStatus.FINISHED,
        outcome=outcome,
        meta_data=meta_data,
        call_end_time=call_end_time or datetime.now(timezone.utc),
        guard_not_status=LeadCallStatus.FINISHED,
    )

    if not updated_lead:
        # Another concurrent callback already marked this lead FINISHED
        logger.info(
            f"Lead {lead.id} was concurrently finished by another callback, skipping retry."
        )
        dispatcher = get_lead_dispatcher()
        if dispatcher:
            await dispatcher.on_channel_freed()
        return None

    # --- 8. Retry if needed (outbound only, retryable outcomes) ---
    if (
        outcome in ("BUSY", "NO_ANSWER", "FAILED")
        and lead.call_direction == CallDirection.OUTBOUND
    ):
        config = await _get_lead_config(lead)
        if config:
            await _retry_call(lead, config, outcome)

    # --- 9. Channel freed — trigger next waiting lead ---
    dispatcher = get_lead_dispatcher()
    if dispatcher:
        await dispatcher.on_channel_freed()

    return updated_lead


# Backward-compatible alias for provider completion callbacks
handle_call_completion = handle_call_ended


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

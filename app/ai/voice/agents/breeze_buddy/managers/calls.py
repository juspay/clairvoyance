"""
Call lifecycle helpers — shared between the dispatch workers and the
webhook handlers.

Historical note: this module used to host ``process_backlog_leads`` (the
cron-driven dispatch loop). That function was deleted when the
event-driven dispatcher replaced it; the helpers (``_get_lead_config``,
``_is_within_calling_hours``, ``_get_available_number``, ``_acquire_number``,
``_release_number``, ``_run_pre_checks_for_lead``, ``_retry_call``) are now
called from ``app.ai.voice.agents.breeze_buddy.dispatch.worker``. See
docs/BACKLOG_DISPATCHER_REDESIGN.md.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

# Dispatch imports use submodule paths (not the ``dispatch`` package) to avoid
# the circular import via ``dispatch/__init__.py`` -> ``dispatch.worker`` ->
# ``managers.calls``. The submodules below have no dependency on this file.
from app.ai.voice.agents.breeze_buddy.dispatch.alerts import raise_orphan_webhook
from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    release_channel_token,
)
from app.ai.voice.agents.breeze_buddy.dispatch.queue import (
    is_dispatchable,
    schedule_lead,
)
from app.ai.voice.agents.breeze_buddy.managers.pre_checks import run_pre_checks
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
from app.ai.voice.agents.breeze_buddy.template.types import (
    TemplateModel,
)
from app.ai.voice.agents.breeze_buddy.utils.common import send_webhook_with_retry
from app.core.config.static import (
    UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor import (
    acquire_lock_on_lead_by_id,
    create_lead_call_tracker,
    decrement_outbound_number_channels,
    get_call_execution_config_by_merchant_id,
    get_lead_by_call_id,
    get_leads_by_status_and_time_before,
    get_outbound_number_based_on_status_and_provider,
    get_outbound_number_by_id,
    increment_outbound_number_channels,
    release_lock_on_lead_by_id,
    update_lead_call_completion_details,
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
    Resolve the outbound number to dial from. Returns None only for
    permanent / semi-permanent failures:

      - template.outbound_number_id points at a row that doesn't exist
      - the row's status is not AVAILABLE (manually disabled, or — for
        Twilio's legacy 1-bit gate — currently IN_USE on another call)
      - fallback search found nothing for this reseller/merchant pool

    Capacity exhaustion is NOT a reason to return None: the Redis
    channel-token semaphore is now the authoritative gate (see
    ``dispatch/channel_semaphore.py``) and is checked one step downstream
    in the worker. The old Exotel ``channels < maximum_channels`` check
    here was redundant with that semaphore and caused permanent
    misconfigurations and transient capacity issues to be indistinguishable
    to the caller — both surfaced as None and were retried forever every
    10s.

    Callers should treat a None return as a terminal condition for the
    lead (mark FINISHED + alert), not as something to retry.
    """

    number = None

    if template and template.outbound_number_id:
        logger.info(
            f"Using new approach: template {config.template} has outbound_number_id {template.outbound_number_id}"
        )
        outbound_number = await get_outbound_number_by_id(template.outbound_number_id)

        if outbound_number and outbound_number.status == OutboundNumberStatus.AVAILABLE:
            number = outbound_number
        elif outbound_number is None:
            logger.error(
                f"_get_available_number: template {config.template} references "
                f"outbound_number_id {template.outbound_number_id} which does not "
                "exist. This is a misconfiguration that will not self-heal."
            )
        else:
            logger.warning(
                f"_get_available_number: outbound number {outbound_number.id} "
                f"is in status {outbound_number.status.value} (not AVAILABLE) "
                f"for template {config.template}."
            )

    else:
        logger.info(
            f"Using backward compatible approach for reseller "
            f"{config.reseller_id}, shop {config.merchant_id}: scanning the "
            f"unassigned-default pool (outbound_number rows with reseller_id "
            f"and merchant_id both NULL) on provider "
            f"{config.calling_provider}."
        )

        # Get all available numbers
        all_available_numbers = await get_outbound_number_based_on_status_and_provider(
            OutboundNumberStatus.AVAILABLE, config.calling_provider
        )

        # Legacy fallback: any number with no explicit reseller/merchant
        # assignment serves as a shared default. The caller's reseller_id /
        # merchant_id from `config` are intentionally NOT used as a filter
        # here — the fallback is the unassigned-default pool, not a per-
        # tenant pool. See raise_no_outbound_number() in dispatch/alerts.py
        # for the operator guidance that matches this behavior.
        matching_numbers = [
            n
            for n in all_available_numbers
            if n.reseller_id is None and n.merchant_id is None
        ]

        if matching_numbers:
            number = matching_numbers[0]

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
        retry_id = str(uuid.uuid4())
        created = await create_lead_call_tracker(
            id=retry_id,
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

        # Only ZADD onto the schedule if the DB insert actually landed. If
        # the accessor returned None (insert failed and was swallowed by the
        # accessor's try/except), scheduling retry_id would have the worker
        # repeatedly BLPOP a ghost lead and drop — and the original lead
        # would silently lose its retry. Loudly log instead so the failure
        # surfaces in monitoring.
        if created is None:
            logger.error(
                f"Retry insert failed for lead {lead.id} "
                f"(attempt {lead.attempt_count + 1}); no retry scheduled"
            )
            return

        # Event-driven dispatch: ZADD the retry onto the schedule. Best-effort
        # — DB is authoritative; reconciler heals dropped ZADDs.
        # Gated on execution_mode — only telephony retries flow through the
        # worker's make_call path. DAILY retries (if any) are handled by the
        # web-mode flow, not by phantom-dialling via Plivo/Twilio.
        # See docs/BACKLOG_DISPATCHER_REDESIGN.md §4 (retry semantics).
        if is_dispatchable(lead.execution_mode):
            await schedule_lead(lead_id=retry_id, next_attempt_at=next_attempt_at)


async def reconcile_stuck_processing_leads():
    """
    Cleans up leads that are stuck in the PROCESSING state — call placed
    but no call-end webhook received within 10 minutes. Closes the row
    with outcome=UNKNOWN, releases the outbound number + channel token,
    and triggers a retry where applicable.

    Registered on ``BackgroundTaskScheduler``; the scheduler's distributed
    lock guarantees only one pod runs this per interval. See
    docs/BACKLOG_DISPATCHER_REDESIGN.md §2 Plane 5.
    """
    logger.info("Cleaning up stuck leads...")
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    stale_leads = await get_leads_by_status_and_time_before(
        LeadCallStatus.PROCESSING, stale_time, include_locked=True
    )

    logger.info(f"Found {len(stale_leads)} stuck leads to clean up.")

    for lead in stale_leads:
        locked_lead = None
        try:
            # Forcefully acquire the lock — the lead may still be marked
            # is_locked=TRUE from a crashed pod. Safe here because the
            # BackgroundTaskScheduler distributed lock ensures only one
            # reconciler runs at a time, and we only reach this path after
            # a 10-minute staleness timeout.
            locked_lead = await acquire_lock_on_lead_by_id(
                lead.id, expected_status=LeadCallStatus.PROCESSING, force=True
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
                    # Event-driven dispatch: return token to channel semaphore.
                    await release_channel_token(outbound_number.id)

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
        # §7.1 detection: orphan webhook = a call exists at the provider that
        # has no matching lead row, almost always the symptom of a worker
        # crashing between provider.make_call and the DB UPDATE.
        await raise_orphan_webhook(call_id=call_id, source="call_completion")
        return

    # Always release outbound number (including transfers — bot leaves, cleanup happens here)
    if lead.outbound_number_id:
        outbound_number = await get_outbound_number_by_id(lead.outbound_number_id)
        if outbound_number:
            await _release_number(outbound_number.id, outbound_number.provider)
            # Event-driven dispatch: return a token to the channel semaphore.
            # Idempotent in aggregate — reconcile_channel_tokens trims any
            # over-count caused by duplicate webhooks within 60s.
            await release_channel_token(outbound_number.id)
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

    config = None
    if lead.template == "IVR-OPTIONS":
        logger.info(
            f"Skipping config check for template IVR-OPTIONS for lead {lead.id}"
        )
    else:
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
        config
        and outcome in ["BUSY", "NO_ANSWER"]
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
        # §7.1 detection — see handle_call_completion for context.
        await raise_orphan_webhook(call_id=call_id, source="unanswered")
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
            # Event-driven dispatch: return token to channel semaphore.
            await release_channel_token(outbound_number.id)
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

    config = None
    if lead.template == "IVR-OPTIONS":
        logger.info(
            f"Skipping config check for template IVR-OPTIONS for lead {lead.id}"
        )
    else:
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
        config
        and lead.call_direction == CallDirection.OUTBOUND
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

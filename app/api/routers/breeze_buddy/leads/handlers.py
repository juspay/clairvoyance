"""
Business logic handlers for lead operations.
All handlers perform database operations and enforce business rules.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict
from uuid import uuid4

from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.dispatch import (
    cancel_scheduled_lead,
    is_dispatchable,
    schedule_lead,
)
from app.ai.voice.agents.breeze_buddy.services.daily.recording import (
    download_call_recording as download_call_recording_daily,
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
from app.ai.voice.agents.breeze_buddy.types.models import (
    CallRecordingResult,
    LeadCancellation,
    PushLeadRequest,
)
from app.ai.voice.agents.breeze_buddy.utils.common import (
    get_validation_error_message,
    validate_payload,
)
from app.ai.voice.agents.breeze_buddy.utils.language_utils.language_detector import (
    LANGUAGE_NAMES,
    SHORT_TO_FULL_LANGUAGE_CODE,
    determine_language_for_call,
)
from app.ai.voice.agents.breeze_buddy.utils.language_utils.translator import (
    translate_transcript_to_english,
)
from app.ai.voice.agents.breeze_buddy.utils.tts_utils.tts_provider_selector import (
    determine_tts_provider_for_call,
)
from app.core.logger import logger
from app.database.accessor import (
    append_metadata_field,
    create_lead_call_tracker,
    get_call_execution_config_by_merchant_id,
    get_lead_by_call_id,
    get_lead_by_id,
    get_outbound_number_by_id,
    get_template_by_id,
    get_template_by_merchant,
    handle_lead_abort,
    is_number_blacklisted,
)
from app.database.accessor.breeze_buddy.dispatch import (
    update_lead_next_attempt_at_now,
)
from app.schemas import ExecutionMode, LeadCallStatus, UserInfo
from app.schemas.breeze_buddy.core import LeadCallTracker

from .rbac import validate_recording_access


async def get_lead_handler(lead_id: str, current_user: UserInfo) -> Dict:
    """
    Get a lead by ID with all fields.

    Args:
        lead_id: Lead UUID
        current_user: Current authenticated user

    Returns:
        Complete lead object with all fields including metaData (contains transcription)

    Raises:
        HTTPException: 404 if not found
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) requesting lead: {lead_id}"
    )

    try:
        lead = await get_lead_by_id(lead_id)

        if not lead:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Lead not found for ID: {lead_id}",
            )

        return lead.model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting lead {lead_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(e)}",
        )


async def push_lead_handler(req: PushLeadRequest, current_user: UserInfo) -> Dict:
    """
    Push a new lead for processing.

    This is the main entry point for creating leads. It:
    1. Validates the template exists
    2. Validates payload against template schema
    3. Gets call execution config
    4. Creates lead call tracker record

    Args:
        req: Lead push request
        current_user: Current authenticated user

    Returns:
        Success response with lead tracker ID

    Raises:
        HTTPException: 404 if template/config not found, 400 on validation error
    """

    logger.info(
        f"User {current_user.username} (role: {current_user.role}) pushing lead "
        f"for reseller: {req.reseller_id}, template: {req.template}, template_id: {req.template_id}"
    )

    try:
        # Fetch template: prefer template_id (direct UUID lookup), fall back to name-based
        template = None
        if req.template_id:
            template = await get_template_by_id(req.template_id)
            if template:
                # Enforce that the template belongs to the requested reseller/merchant
                if template.reseller_id != req.reseller_id:
                    logger.warning(
                        "Template ID %s does not belong to reseller %s (found reseller_id=%s)",
                        req.template_id,
                        req.reseller_id,
                        template.reseller_id,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Template not found for reseller: {req.reseller_id} "
                        f"(template_id={req.template_id}, template={req.template})",
                    )
                if (
                    req.merchant_id is not None
                    and template.merchant_id is not None
                    and template.merchant_id != req.merchant_id
                ):
                    logger.warning(
                        "Template ID %s does not belong to merchant %s "
                        "(found merchant_id=%s, reseller_id=%s)",
                        req.template_id,
                        req.merchant_id,
                        template.merchant_id,
                        template.reseller_id,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Template not found for reseller: {req.reseller_id} "
                        f"(template_id={req.template_id}, template={req.template})",
                    )
                # If the template is merchant-specific but request omits merchant_id,
                # inherit the template's merchant_id so downstream config lookup and
                # lead creation are consistent with the resolved template's scope.
                if template.merchant_id is not None and req.merchant_id is None:
                    logger.info(
                        "Inheriting merchant_id=%s from template %s (not provided in request)",
                        template.merchant_id,
                        req.template_id,
                    )
                    req = req.model_copy(update={"merchant_id": template.merchant_id})
        if not template and req.template:
            template = await get_template_by_merchant(
                req.reseller_id, req.merchant_id, req.template
            )

        if not template:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Template not found for reseller: {req.reseller_id} "
                f"(template_id={req.template_id}, template={req.template})",
            )

        # Check if customer phone number is blacklisted
        customer_mobile = req.payload.get("customer_mobile_number")
        if customer_mobile and await is_number_blacklisted(
            customer_mobile, req.reseller_id
        ):
            logger.warning(
                f"Blacklisted number {customer_mobile} rejected for reseller {req.reseller_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Phone number is blacklisted",
            )

        # Validate payload against expected schema if schema exists
        if template and template.expected_payload_schema:
            is_valid, validation_errors = validate_payload(
                req.payload, template.expected_payload_schema
            )

            if not is_valid:
                error_message = get_validation_error_message(validation_errors)
                logger.warning(
                    f"Payload validation failed for reseller {req.reseller_id}: {error_message}"
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=error_message,
                )

            logger.info(f"Payload validation successful for reseller {req.reseller_id}")

        # Get call execution config
        call_execution_configs = await get_call_execution_config_by_merchant_id(
            req.reseller_id, req.merchant_id
        )

        if not call_execution_configs:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Call execution config not found for reseller_id: {req.reseller_id}",
            )

        # Two-step config match: prefer exact template_id, fall back to name
        # (only consider name-match for configs that have no template_id set,
        #  to avoid accidentally picking a config with a conflicting template_id)
        config = None
        if template.id:
            config = next(
                (
                    c
                    for c in call_execution_configs
                    if c.template_id and c.template_id == str(template.id)
                ),
                None,
            )
        if not config:
            config = next(
                (
                    c
                    for c in call_execution_configs
                    if c.template == template.name
                    and (not template.id or not c.template_id)
                ),
                None,
            )

        if not config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Call execution config not found for template: {template.name}",
            )

        uuid = str(uuid4())

        # Calculate next attempt time
        next_attempt_at = datetime.now(timezone.utc) + timedelta(
            seconds=config.initial_offset
        )

        # Prepare payload with reporting webhook URL
        lead_payload = {**req.payload}
        if req.reporting_webhook_url:
            lead_payload["reporting_webhook_url"] = req.reporting_webhook_url

        if req.is_playground:
            # Playground supplies stt_language and tts_provider explicitly —
            # skip LLM-based detection entirely.
            overrides = req.configurations_override or {}
            stt_language = overrides.get("stt_language", "")
            normalized_language = SHORT_TO_FULL_LANGUAGE_CODE.get(
                stt_language, stt_language
            )
            lead_payload["language_name"] = LANGUAGE_NAMES.get(
                normalized_language, "English"
            )
            tts_provider_val = overrides.get("tts_provider")
            if tts_provider_val:
                lead_payload["tts_provider"] = tts_provider_val
        else:
            _, language_name = await determine_language_for_call(
                template.configurations if template else None,
                lead_payload,
                req.request_id,
            )
            lead_payload["language_name"] = language_name

            # Determine TTS provider using LLM if payload-based selection is enabled
            tts_provider = await determine_tts_provider_for_call(
                template.configurations if template else None,
                lead_payload,
                req.request_id,
            )
            if tts_provider:
                lead_payload["tts_provider"] = tts_provider.value

        # Validate that flow override is only used in playground mode
        if req.flow_override and not req.is_playground:
            raise HTTPException(
                status_code=400,
                detail="flow_override can only be provided when is_playground=true",
            )

        # In playground mode, store configurations and flow overrides in metadata
        meta_data = {}
        if req.is_playground:
            meta_data = {
                "playground": True,
                "configurations": req.configurations_override,
            }
            # Store flow override if provided (allows testing flow changes without saving template)
            if req.flow_override:
                meta_data["flow_override"] = req.flow_override

        # Insert lead call tracker record with both template name and template_id
        lead_call_tracker = await create_lead_call_tracker(
            id=uuid,
            reseller_id=req.reseller_id,
            template=template.name,
            template_id=str(template.id),
            merchant_id=req.merchant_id,
            next_attempt_at=next_attempt_at,
            payload=lead_payload,
            attempt_count=0,
            meta_data=meta_data,
            request_id=req.request_id,
            execution_mode=req.execution_mode or ExecutionMode.TELEPHONY,
            status=LeadCallStatus.BACKLOG,
        )

        if not lead_call_tracker:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to add lead call tracker for request_id: {req.request_id}",
            )

        # Event-driven dispatch: drop a scheduling sticky note onto
        # bb:schedule:leads. Best-effort — DB is authoritative; the
        # reconciler heals any dropped ZADDs within 60s.
        # Gated on execution_mode so we only dispatch TELEPHONY leads;
        # DAILY / DAILY_STREAM are handled by separate web-mode flows
        # (customer-initiated room join) and would otherwise cause a
        # phantom Plivo/Twilio dial via the worker's make_call path.
        # See docs/BACKLOG_DISPATCHER_REDESIGN.md §2 Plane 1.
        lead_execution_mode = req.execution_mode or ExecutionMode.TELEPHONY
        if next_attempt_at is not None and is_dispatchable(lead_execution_mode):
            await schedule_lead(lead_id=uuid, next_attempt_at=next_attempt_at)

        logger.info(f"Lead call tracker {req.request_id} added to queue with ID {uuid}")

        return {
            "status": "queued",
            "lead_call_tracker_id": uuid,
            "order_id": req.request_id,
            "message": "Call request added to queue for processing",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing lead push request: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing lead push request: {str(e)}",
        )


async def get_call_recording_handler(
    call_sid: str, current_user: UserInfo
) -> CallRecordingResult:
    """
    Get call recording audio file for a given call SID or Daily room name.

    This handler:
    1. Fetches lead by call_id to get reseller_id and merchant_id
    2. Validates RBAC access (fails fast before fetching recording)
    3. For Daily mode leads: downloads via Daily API access-link or GCS
    4. For telephony leads: gets provider from outbound number and downloads

    Args:
        call_sid: The call SID (telephony) or room name (Daily) to fetch recording for
        current_user: Current authenticated user

    Returns:
        CallRecordingResult with audio file and whether it's a Daily recording

    Raises:
        HTTPException: 404 if not found, 400 if unsupported provider, 502 if download fails
    """
    logger.info(
        f"User {current_user.id} requesting call recording for call_sid: {call_sid}"
    )

    # Step 1: Get lead by call_id for RBAC validation
    lead = await get_lead_by_call_id(call_sid)

    if not lead:
        logger.warning(f"No lead found for call_sid: {call_sid}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recording not found for call_sid: {call_sid}",
        )

    # Step 2: RBAC validation (fails fast before fetching recording)
    if not lead.reseller_id:
        logger.error(f"Lead {call_sid} has no reseller_id - data integrity issue")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recording not found for call_sid: {call_sid}",
        )

    validate_recording_access(
        current_user, call_sid, lead.reseller_id, lead.merchant_id
    )

    # Step 3: If this is a Daily mode lead, use on-demand API lookup (no recording_url needed)
    if lead.execution_mode in (ExecutionMode.DAILY, ExecutionMode.DAILY_TEST):
        if not lead.call_id:
            logger.warning(f"No room name (call_id) for Daily lead: {call_sid}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recording not found for call_sid: {call_sid}",
            )

        audio_file = await download_call_recording_daily(lead.call_id)
        if not audio_file:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to download recording from Daily",
            )

        audio_file.seek(0)
        return CallRecordingResult(audio_file=audio_file, is_daily=True)

    # Step 4: Check if recording URL exists (required for telephony only)
    if not lead.recording_url:
        logger.warning(f"No recording URL for call_sid: {call_sid}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recording not found for call_sid: {call_sid}",
        )

    # Step 5: Get provider from outbound number (telephony only)
    if not lead.outbound_number_id:
        logger.error(f"No outbound number ID for call_sid: {call_sid}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unable to determine call provider for call_sid: {call_sid}",
        )

    outbound_number = await get_outbound_number_by_id(lead.outbound_number_id)
    if not outbound_number:
        logger.error(f"Outbound number not found: {lead.outbound_number_id}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unable to determine call provider for call_sid: {call_sid}",
        )

    call_provider = outbound_number.provider.value

    # Step 6: Fetch audio with provider-specific credentials
    logger.info(f"Fetching recording from URL: {lead.recording_url}")

    if call_provider.upper() == "TWILIO":
        audio_file = await download_call_recording_twilio(lead.recording_url, call_sid)
    elif call_provider.upper() == "EXOTEL":
        audio_file = await download_call_recording_exotel(lead.recording_url, call_sid)
    elif call_provider.upper() == "PLIVO":
        audio_file = await download_call_recording_plivo(lead.recording_url, call_sid)
    else:
        logger.error(f"Unsupported provider: {call_provider}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported provider: {call_provider}",
        )

    if not audio_file:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to download recording from provider: {call_provider}",
        )

    audio_file.seek(0)
    return CallRecordingResult(audio_file=audio_file, is_daily=False)


async def delete_lead_handler(
    leads: list[LeadCancellation], current_user: UserInfo
) -> Dict:
    """
    Delete (abort) multiple leads by IDs with individual cancellation reasons.

    This handler:
    1. Fetches each lead to validate it exists
    2. Checks lead state (PROCESSING, FINISHED, BACKLOG, RETRY)
    3. Attempts to abort leads in abortable states
    4. Returns unified response with status for each lead

    Args:
        leads: List of LeadCancellation objects containing lead_id and cancellation_reason pairs
        current_user: Current authenticated user

    Returns:
        Unified response with individual status for each lead:
        {
            "leads": [
                {
                    "id": "lead_uuid",
                    "status": "success|error|already_aborted|processing",
                    "message": "descriptive message"
                },
                ...
            ]
        }

    Raises:
        HTTPException: 500 on unexpected system failure
    """
    lead_ids = [lead.lead_id for lead in leads]
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) requesting to delete leads: {lead_ids}"
    )

    lead_responses = []

    try:
        for lead_cancellation in leads:
            lead_id = lead_cancellation.lead_id
            cancellation_reason = lead_cancellation.cancellation_reason

            try:
                # Get lead to validate existence
                lead = await get_lead_by_id(lead_id)

                if not lead:
                    lead_responses.append(
                        {
                            "id": lead_id,
                            "status": "error",
                            "message": f"Lead not found",
                        }
                    )
                    continue

                # Check if lead is already aborted
                if lead.status.value == "FINISHED":
                    logger.info(
                        f"Lead {lead_id} is already finished with outcome: {lead.outcome}"
                    )
                    lead_responses.append(
                        {
                            "id": lead_id,
                            "status": "error",
                            "message": f"Cannot abort - already finished with outcome: {lead.outcome}",
                        }
                    )
                    continue

                # Check if lead is in PROCESSING state (call in progress)
                if lead.status.value == "PROCESSING":
                    logger.warning(
                        f"Cannot abort lead {lead_id} - call is currently in progress"
                    )
                    lead_responses.append(
                        {
                            "id": lead_id,
                            "status": "processing",
                            "message": "Cannot abort - call is currently in progress",
                        }
                    )
                    continue

                # Abort the lead (only BACKLOG and RETRY leads should reach here)
                aborted_lead = await handle_lead_abort(lead_id, cancellation_reason)

                if not aborted_lead:
                    logger.error(f"Failed to abort lead {lead_id}")
                    lead_responses.append(
                        {
                            "id": lead_id,
                            "status": "error",
                            "message": "Failed to abort - lead may have transitioned to another state",
                        }
                    )
                    continue

                logger.info(
                    f"Lead {lead_id} successfully aborted by user {current_user.username}"
                )

                # Event-driven dispatch: ZREM from bb:schedule:leads so the
                # promoter doesn't pull a zombie. Safe if not present.
                await cancel_scheduled_lead(lead_id)

                lead_responses.append(
                    {
                        "id": lead_id,
                        "status": "success",
                        "message": "Lead has been successfully aborted",
                    }
                )

            except Exception as e:
                logger.error(f"Error deleting lead {lead_id}: {e}", exc_info=True)
                lead_responses.append(
                    {
                        "id": lead_id,
                        "status": "error",
                        "message": f"Unexpected error: {str(e)}",
                    }
                )

        return {"leads": lead_responses}

    except Exception as e:
        logger.error(f"Error deleting leads: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error while deleting leads: {str(e)}",
        )


async def translate_lead_transcript_handler(
    lead_id: str, lead: LeadCallTracker, current_user: UserInfo
) -> Dict:
    """
    Translate a lead's transcription to English.

    If a cached translation (transcription_en) already exists in meta_data,
    returns it immediately. Otherwise, translates via Gemini and caches the
    result in the DB.

    Args:
        lead_id: Lead UUID
        lead: Already-fetched lead object (avoids duplicate DB call)
        current_user: Current authenticated user

    Returns:
        Dict with translated messages

    Raises:
        HTTPException: 400 if no transcription, 500 if translation fails
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) "
        f"requesting translation for lead: {lead_id}"
    )

    try:
        meta_data = lead.metaData or {}

        # Check for cached translation
        if meta_data.get("transcription_en"):
            logger.info(f"Returning cached translation for lead: {lead_id}")
            return {
                "lead_id": lead_id,
                "transcription_en": meta_data["transcription_en"],
                "cached": True,
            }

        # Get original transcription
        transcription = meta_data.get("transcription")
        if not transcription:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No transcription found for this lead",
            )

        # Handle both list and object formats
        if isinstance(transcription, list):
            messages = transcription
        elif isinstance(transcription, dict) and "messages" in transcription:
            messages = transcription["messages"]
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Transcription format not supported",
            )

        if not messages:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Transcription has no messages to translate",
            )

        # Translate via Gemini
        translated_messages = await translate_transcript_to_english(messages)

        if not translated_messages:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Translation failed. Please try again later.",
            )

        # Cache in DB (fire-and-forget — don't block the response)
        async def _cache_translation():
            try:
                await append_metadata_field(
                    lead_id, {"transcription_en": translated_messages}
                )
            except Exception as e:
                logger.warning(
                    f"Translation succeeded but failed to cache for lead: {lead_id}. "
                    f"Error: {e}"
                )

        asyncio.create_task(_cache_translation())

        return {
            "lead_id": lead_id,
            "transcription_en": translated_messages,
            "cached": False,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error translating transcript for lead {lead_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error during translation: {str(e)}",
        )


async def dispatch_now_lead_handler(lead_id: str, current_user: UserInfo) -> Dict:
    """
    Manual operator endpoint: force-schedule a BACKLOG lead to fire now.

    Goes through every normal dispatch guard (pre-checks, calling-hours,
    rate-limit, channel capacity, idempotency CAS). No bypass code path.

    See docs/BACKLOG_DISPATCHER_REDESIGN.md §2 Plane 1 "Manual dispatch".

    Status guards:
      PROCESSING       -> 409 (call already in flight)
      FINISHED         -> 400 (re-create a new lead for re-attempt)
      is_locked=TRUE   -> 409 (locked by another dispatcher)
      BACKLOG          -> proceed

    Returns:
        {"status": "queued", "lead_id": ..., "expected_dispatch_within_ms": 200}
    """
    lead = await get_lead_by_id(lead_id)
    if not lead:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Lead not found for ID: {lead_id}",
        )

    if lead.status == LeadCallStatus.PROCESSING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Lead is currently being dispatched",
        )
    if lead.status == LeadCallStatus.FINISHED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Lead is finished; create a new lead for re-attempt",
        )
    if getattr(lead, "is_locked", False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Lead is locked by another dispatcher",
        )
    if not is_dispatchable(lead.execution_mode):
        # /dispatch-now is a TELEPHONY-only endpoint. DAILY / DAILY_STREAM
        # leads are handled by web-mode flows (customer-initiated room
        # join) and would phantom-dial via the worker if scheduled here.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Lead execution_mode '{lead.execution_mode.value}' is not "
                "dispatchable via /dispatch-now (telephony-only endpoint)"
            ),
        )

    now = datetime.now(timezone.utc)
    updated = await update_lead_next_attempt_at_now(lead_id, now)
    if not updated:
        # CAS lost — status changed between the load and the update.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Lead state changed during dispatch-now; please retry",
        )

    # No jitter — operator intent is literally "now".
    await schedule_lead(lead_id=lead_id, next_attempt_at=now, jitter_ms=0)

    logger.info(
        f"User {current_user.username} (role: {current_user.role}) "
        f"manually dispatched lead {lead_id}"
    )

    return {
        "status": "queued",
        "lead_id": lead_id,
        "expected_dispatch_within_ms": 200,
    }

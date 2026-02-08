"""
Business logic handlers for lead operations.
All handlers perform database operations and enforce business rules.
"""

from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Dict
from uuid import uuid4

from fastapi import HTTPException, status

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
    LeadCancellation,
    PushLeadRequest,
)
from app.ai.voice.agents.breeze_buddy.utils.common import (
    get_validation_error_message,
    validate_payload,
)
from app.ai.voice.agents.breeze_buddy.utils.language_utils.language_detector import (
    determine_language_for_call,
)
from app.core.logger import logger
from app.database.accessor import (
    create_lead_call_tracker,
    get_call_execution_config_by_merchant_id,
    get_lead_by_call_id,
    get_lead_by_id,
    get_outbound_number_by_id,
    get_template_by_merchant,
    handle_lead_abort,
)
from app.schemas import ExecutionMode, LeadCallStatus, UserInfo

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
        f"for merchant: {req.merchant}, template: {req.template}"
    )

    try:
        # Fetch template to get expected payload schema
        template = await get_template_by_merchant(
            req.merchant, req.identifier, req.template
        )

        if not template:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Template '{req.template}' not found for merchant: {req.merchant}",
            )

        # Validate payload against expected schema if schema exists
        if template.expected_payload_schema:
            is_valid, validation_errors = validate_payload(
                req.payload, template.expected_payload_schema
            )

            if not is_valid:
                error_message = get_validation_error_message(validation_errors)
                logger.warning(
                    f"Payload validation failed for merchant {req.merchant}: {error_message}"
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=error_message,
                )

            logger.info(f"Payload validation successful for merchant {req.merchant}")

        # Get call execution config
        call_execution_configs = await get_call_execution_config_by_merchant_id(
            req.merchant, req.identifier
        )

        if not call_execution_configs:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Call execution config not found for merchant_id: {req.merchant}",
            )

        config = next(
            (c for c in call_execution_configs if c.template == req.template), None
        )

        if not config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Call execution config not found for template: {req.template}",
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

        # Determine language using unified helper function
        _, language_name = await determine_language_for_call(
            template.configurations if template else None,
            lead_payload,
            req.request_id,
        )

        # Add language name to payload for use during call (agent.py uses this)
        lead_payload["language_name"] = language_name

        # Insert lead call tracker record with both template name and template_id
        lead_call_tracker = await create_lead_call_tracker(
            id=uuid,
            merchant_id=req.merchant,
            template=req.template,
            template_id=str(template.id),
            shop_identifier=req.identifier,
            next_attempt_at=next_attempt_at,
            payload=lead_payload,
            attempt_count=0,
            meta_data={},
            request_id=req.request_id,
            execution_mode=req.execution_mode or ExecutionMode.TELEPHONY,
            status=LeadCallStatus.BACKLOG,
        )

        if not lead_call_tracker:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to add lead call tracker for request_id: {req.request_id}",
            )

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


async def get_call_recording_handler(call_sid: str, current_user: UserInfo) -> BytesIO:
    """
    Get call recording audio file for a given call SID.

    This handler:
    1. Fetches lead by call_id to get merchant_id and shop_identifier
    2. Validates RBAC access (fails fast before fetching recording)
    3. Gets provider from outbound number
    4. Downloads audio from the appropriate provider (Twilio/Exotel)

    Args:
        call_sid: The call SID to fetch recording for
        current_user: Current authenticated user

    Returns:
        BytesIO audio file

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
    validate_recording_access(
        current_user, call_sid, lead.merchant_id, lead.shop_identifier
    )

    # Step 3: Check if recording URL exists
    if not lead.recording_url:
        logger.warning(f"No recording URL for call_sid: {call_sid}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recording not found for call_sid: {call_sid}",
        )

    # Step 4: Get provider from outbound number
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

    # Step 5: Fetch audio with provider-specific credentials
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
    return audio_file


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

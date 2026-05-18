"""
Modern RESTful lead management endpoints with RBAC.

This module provides clean REST API endpoints for managing leads (call requests).
Leads represent individual call attempts to customers.

Endpoints:
- POST   /leads              - Push new lead for processing
- GET    /leads/{id}         - Get lead details by ID
- DELETE /leads/{id}         - Delete (abort) a lead by ID
- GET    /leads/recording/{call_sid} - Get call recording audio (telephony & Daily)

For backward compatibility, old endpoints are available in deprecated/leads.py
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.ai.voice.agents.breeze_buddy.types.models import (
    CancelLeadRequest,
    PushLeadRequest,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.logger import logger
from app.database.accessor import get_lead_by_id
from app.schemas import UserInfo

from .handlers import (
    delete_lead_handler,
    dispatch_now_lead_handler,
    get_call_recording_handler,
    get_lead_handler,
    push_lead_handler,
    translate_lead_transcript_handler,
)
from .rbac import (
    validate_lead_access,
    validate_lead_read_access,
)

router = APIRouter()


@router.post("/leads", status_code=status.HTTP_201_CREATED)
async def push_lead(
    req: PushLeadRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Push a new lead for processing.

    This endpoint receives lead details (customer info, order data, etc.) and queues
    them for automated call processing. The system will:
    1. Validate the payload against the template's expected schema
    2. Get the call execution configuration
    3. Schedule the call based on initial_offset
    4. Return a lead_call_tracker_id for tracking

    Permissions:
    - Admin: Can push leads for any reseller/merchant
    - Reseller: Can push leads for own merchants only

    Request Body:
        {
            "reseller_id": "reseller_123",
            "template": "order-confirmation",
            "merchant_id": "merchant_123",
            "request_id": "order_456",
            "reporting_webhook_url": "https://example.com/webhook",
            "payload": {
                "customer_name": "John Doe",
                "customer_mobile_number": "+1234567890",
                "shop_name": "My Shop",
                "total_price": "100.00",
                "items": [...]
            }
        }

    Returns:
        {
            "status": "queued",
            "lead_call_tracker_id": "uuid",
            "order_id": "order_456",
            "message": "Call request added to queue for processing"
        }
    """

    if not req.reseller_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reseller_id is required",
        )
    # RBAC: Check permission to push leads for this reseller/shop
    validate_lead_access(
        current_user, req.reseller_id, req.merchant_id, operation="push leads for"
    )

    return await push_lead_handler(req, current_user)


@router.get("/leads/{lead_id}")
async def get_lead(
    lead_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Get lead details by ID.

    Returns lead information excluding sensitive fields like metadata,
    cost, lock status, and internal timestamps.

    Path Parameters:
    - lead_id: Lead UUID

    RBAC:
    - Admin: Can access any lead
    - Merchant: Can only access leads for own merchants/shops

    Returns:
        Lead object (sanitized) if found
        404 if not found or access denied
    """
    # Get lead from handler
    lead = await get_lead_by_id(lead_id)

    if not lead:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Lead not found for ID: {lead_id}",
        )

    # RBAC: Check access (returns 404 to avoid leaking existence)
    validate_lead_read_access(current_user, lead, operation="access")

    # Get sanitized lead data
    return await get_lead_handler(lead_id, current_user)


@router.post("/leads/{lead_id}/dispatch-now")
async def dispatch_now_lead(
    lead_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Manually dispatch a BACKLOG lead immediately (operator endpoint).

    Force-schedules the lead's ``next_attempt_at`` to NOW and ZADDs it onto
    the schedule. Goes through every normal dispatch guard (pre-checks,
    calling-hours, rate-limit, channel capacity, idempotency). No bypass
    code path — operator preferences override scheduling, not correctness.

    See docs/BACKLOG_DISPATCHER_REDESIGN.md §2 Plane 1.

    Path Parameters:
    - lead_id: Lead UUID

    Status guards:
    - PROCESSING: 409 (call already in flight)
    - FINISHED: 400 (create a new lead for re-attempt)
    - is_locked=TRUE: 409 (locked by another dispatcher)

    RBAC:
    - Admin: Can dispatch any lead
    - Merchant: Can only dispatch own merchants/shops

    Returns:
        {"status": "queued", "lead_id": "uuid", "expected_dispatch_within_ms": 200}
    """
    lead = await get_lead_by_id(lead_id)
    if not lead:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Lead not found for ID: {lead_id}",
        )

    # RBAC: returns 404 on access denial to avoid leaking existence.
    validate_lead_read_access(current_user, lead, operation="dispatch")

    return await dispatch_now_lead_handler(lead_id, current_user)


@router.post("/leads/{lead_id}/translate")
async def translate_lead_transcript(
    lead_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Translate a lead's call transcription to English.

    Uses Gemini LLM to translate the transcription from its original language
    (Hindi, Tamil, Telugu, Kannada, etc.) to English. The translation is cached
    in the lead's meta_data as 'transcription_en' for subsequent requests.

    Path Parameters:
    - lead_id: Lead UUID

    RBAC:
    - Admin: Can translate any lead's transcription
    - Merchant: Can only translate transcriptions for authorized leads

    Returns:
        {
            "lead_id": "uuid",
            "transcription_en": [{role, content}, ...],
            "cached": true/false
        }
        404 if lead not found or access denied
        400 if no transcription exists
    """
    lead = await get_lead_by_id(lead_id)

    if not lead:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Lead not found for ID: {lead_id}",
        )

    # RBAC: Check access (returns 404 to avoid leaking existence)
    validate_lead_read_access(current_user, lead, operation="translate")

    return await translate_lead_transcript_handler(lead_id, lead, current_user)


@router.get("/leads/recording/{call_sid}")
async def get_call_recording(
    call_sid: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Fetches call recording audio for a given call SID or Daily room name.

    Automatically detects the mode (telephony vs Daily) and provider,
    then uses appropriate credentials to fetch the recording.
    Returns the audio file as a streaming response.

    RBAC:
    - Admin: Can access any recording
    - Merchant/Shop: Can only access recordings for authorized merchants/shops

    Path Parameters:
    - call_sid: The call SID (telephony) or room name (Daily) to fetch recording for

    Returns:
        Audio file stream (MP3 for telephony, MP4 for Daily)
        404 if not found or access denied
    """
    try:
        result = await get_call_recording_handler(call_sid, current_user)

        if result.is_daily:
            media_type = "audio/mp4"
            extension = "mp4"
        else:
            media_type = "audio/mpeg"
            extension = "mp3"

        return StreamingResponse(
            result.audio_file,
            media_type=media_type,
            headers={
                "Content-Disposition": f'inline; filename="recording_{call_sid}.{extension}"'
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching call recording: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error while fetching recording: {str(e)}",
        ) from e


@router.post("/lead/abort", status_code=status.HTTP_200_OK)
async def delete_lead(
    request: CancelLeadRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Delete (abort) multiple leads by IDs with individual cancellation reasons.

    This endpoint aborts leads by setting their status to FINISHED and outcome to ABORT.
    Each lead can have its own cancellation reason.
    The lead records are not physically deleted but marked as aborted in the database.

    Request Body:
    {
        "leads": [
            {
                "lead_id": "uuid1",
                "cancellation_reason": "Customer requested cancellation"
            },
            {
                "lead_id": "uuid2",
                "cancellation_reason": "Duplicate order"
            }
        ]
    }

    RBAC:
    - Admin: Can delete any lead
    - Merchant: Can only delete leads for own merchants/shops

    Returns:
        {
            "status": "success",
            "message": "Processed N lead(s)",
            "results": [
                {
                    "lead_id": "uuid1",
                    "status": "aborted",
                    "outcome": "ABORT",
                    "cancellation_reason": "Customer requested cancellation",
                    "message": "Lead uuid1 has been aborted"
                },
                {
                    "lead_id": "uuid2",
                    "status": "aborted",
                    "outcome": "ABORT",
                    "cancellation_reason": "Duplicate order",
                    "message": "Lead uuid2 has been aborted"
                }
            ],
            "errors": [
                {
                    "lead_id": "uuid3",
                    "error": "Lead not found for ID: uuid3"
                }
            ]
        }

    Raises:
        404 if not found or access denied
        500 if abort operation fails
    """
    # Validate RBAC for each lead
    for lead_cancellation in request.leads:
        lead = await get_lead_by_id(lead_cancellation.lead_id)
        if lead:
            # RBAC: Check access (returns 404 to avoid leaking existence)
            validate_lead_read_access(current_user, lead, operation="delete")

    # Delete (abort) the leads
    return await delete_lead_handler(request.leads, current_user)

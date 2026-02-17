"""
Telephony provider callback endpoints.

This module provides webhook endpoints for telephony providers (Twilio, Exotel, Plivo)
to send call status updates and recording URLs.

Endpoints:
- GET    /{provider}/callback/details  - Receive call details (Exotel)
- POST   /{provider}/callback/details  - Receive call details (Twilio/Plivo)
- POST   /{provider}/callback/status   - Receive call status updates

Note: The answer webhook (previously /plivo/answer and /exotel/voicebot-url)
has been unified into /{provider}/answer in the answer module.
Plivo IVR is now handled agent-side (in-band over WebSocket) like Exotel,
so the /plivo/ivr-select endpoint is no longer needed.
"""

from fastapi import APIRouter, BackgroundTasks, Request

from .handlers import (
    handle_callback_details_get,
    handle_callback_details_post,
    handle_callback_status,
)

router = APIRouter()


@router.get("/{provider}/callback/details")
async def callback_details(
    request: Request, provider: str, background_tasks: BackgroundTasks
):
    """
    Webhook endpoint for receiving call details via GET (Exotel).

    This endpoint receives call recording URLs from Exotel via query parameters.
    The recording is downloaded and processed asynchronously.

    Path Parameters:
        provider: Telephony provider name (currently only "exotel" supported)

    Query Parameters:
        CallSid: Unique identifier for the call
        Stream[RecordingUrl]: URL to download the call recording

    Returns:
        200 OK response (webhook acknowledgment)

    Raises:
        404: If provider is not "exotel"

    Example:
        GET /exotel/callback/details?CallSid=abc123&Stream[RecordingUrl]=https://...
    """
    return await handle_callback_details_get(request, provider, background_tasks)


@router.post("/{provider}/callback/details")
async def callback_details_post(
    request: Request, provider: str, background_tasks: BackgroundTasks
):
    """
    Webhook endpoint for receiving call details via POST (Twilio).

    This endpoint receives call recording URLs from Twilio via form data.
    The recording is downloaded and processed asynchronously.

    Path Parameters:
        provider: Telephony provider name (currently only "twilio" supported)

    Form Data:
        CallSid: Unique identifier for the call
        RecordingUrl: URL to download the call recording

    Returns:
        200 OK response (webhook acknowledgment)

    Raises:
        404: If provider is not "twilio"

    Example:
        POST /twilio/callback/details
        Form data: CallSid=abc123&RecordingUrl=https://...
    """
    return await handle_callback_details_post(request, provider, background_tasks)


@router.post("/{provider}/callback/status")
async def callback_status(request: Request, provider: str):
    """
    Webhook endpoint for receiving call status updates.

    This endpoint receives call status updates from telephony providers.
    When a call fails (no-answer, failed, busy), it triggers retry logic.

    Path Parameters:
        provider: Telephony provider name ("twilio" or "exotel")

    Form Data (Twilio):
        CallSid: Unique identifier for the call
        CallStatus: Status of the call (e.g., "completed", "no-answer", "failed", "busy")

    Form Data (Exotel):
        CallSid: Unique identifier for the call
        Status: Status of the call (e.g., "completed", "no-answer", "failed", "busy")

    Returns:
        200 OK response (webhook acknowledgment)

    Behavior:
        - If call_status is "no-answer", "failed", or "busy":
          Triggers retry logic via handle_unanswered_calls()
        - Otherwise: Logs the status and acknowledges

    Example:
        POST /twilio/callback/status
        Form data: CallSid=abc123&CallStatus=no-answer
    """
    return await handle_callback_status(request, provider)

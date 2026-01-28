"""
Telephony provider callback endpoints.

This module provides webhook endpoints for telephony providers (Twilio, Exotel, Plivo)
to send call status updates and recording URLs.

Current Endpoints (maintained for backward compatibility):
- GET    /{provider}/callback/details  - Receive call details (Exotel)
- POST   /{provider}/callback/details  - Receive call details (Twilio)
- POST   /{provider}/callback/status   - Receive call status updates
- POST   /plivo/answer                 - Plivo answer webhook (returns XML)
Ideal RESTful Structure (for future migration):
The current endpoints are provider-agnostic and follow a good pattern.
However, for better RESTful design, consider:

Option 1 - Resource-oriented (recommended for multi-provider):
- POST   /telephony/webhooks/{provider}/call-details   - Receive call details
- POST   /telephony/webhooks/{provider}/call-status    - Receive call status
- GET    /telephony/webhooks/{provider}/call-details   - Receive call details (GET)

Option 2 - Provider-specific namespacing:
- POST   /telephony/twilio/webhooks/call-details       - Twilio call details
- POST   /telephony/twilio/webhooks/call-status        - Twilio call status
- GET    /telephony/exotel/webhooks/call-details       - Exotel call details
- POST   /telephony/exotel/webhooks/call-status        - Exotel call status

Current Structure Assessment:
The existing routes are acceptable because:
1. They clearly indicate the provider via path parameter
2. They differentiate between GET/POST for different providers
3. They separate concerns (details vs status)
4. They're webhook endpoints, so REST purity is less critical

Recommendation: Keep current routes as-is for now since they're already well-designed
and changing them would break existing provider integrations.
"""

from fastapi import APIRouter, BackgroundTasks, Request

from .handlers import (
    handle_callback_details_get,
    handle_callback_details_post,
    handle_callback_status,
    handle_plivo_answer,
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


# Plivo-specific endpoints
@router.post("/plivo/answer")
async def plivo_answer(request: Request):
    """
    Webhook endpoint for Plivo to initiate audio streaming.

    Plivo calls this endpoint when an outbound call is answered.
    Returns XML with Stream element that tells Plivo to connect
    the call to a WebSocket for real-time audio streaming.

    Returns:
        XML Response with Stream element for WebSocket connection

    Example:
        POST /agent/voice/breeze-buddy/plivo/answer
        Response: <Response>...<Stream websocketUrl="wss://..." bidirectional="true"/>...</Response>
    """
    return await handle_plivo_answer(request)

"""
Telephony provider callback endpoints.

This module provides webhook endpoints for telephony providers (Twilio, Exotel, Plivo)
to send call status updates and recording URLs.

Endpoints:
- GET    /{provider}/callback/details                        - Receive call details (Exotel)
- POST   /{provider}/callback/details                        - Receive call details (Twilio/Plivo)
- POST   /{provider}/callback/status                         - Receive call status updates
- POST   /twilio/callback/twiml-fallback - Fallback TwiML when Smart Router is down
- *      /{provider}/callback/transfer/{action}              - Transfer callbacks (GET/POST)
    - dial-up           : Fetch dial-target info (Exotel GET, Plivo POST)
    - conference-end    : Conference ended — resource cleanup (Twilio/Plivo POST)
"""

from fastapi import APIRouter, BackgroundTasks, Request

from .handlers import (
    handle_call_transfer,
    handle_callback_details_get,
    handle_callback_details_post,
    handle_callback_status,
    handle_twilio_twiml_fallback,
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
    Webhook endpoint for receiving call details via POST (Twilio/Plivo).

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
    For transfer calls, if recording URL is present, saves it to metadata.

    Also serves as a backup release mechanism — when a call ends, it notifies
    Smart Router to release the pod (idempotent, safe if already released).

    Path Parameters:
        provider: Telephony provider name ("twilio", "exotel", or "plivo")

    Form Data (Twilio):
        CallSid: Unique identifier for the call
        CallStatus: Status of the call
        RecordingUrl: (Optional) URL to download the call recording

    Form Data (Exotel):
        CallSid: Unique identifier for the call
        Status: Status of the call

    Form Data (Plivo):
        CallUUID: Unique identifier for the call
        CallStatus: Status of the call
        RecordingUrl or Stream[RecordingUrl]: (Optional) URL to download the call recording

    Form Data (Plivo):
        CallUUID: Unique identifier for the call
        CallStatus: Status of the call
        record_url: (Optional) URL to download the call recording

    Returns:
        200 OK response (webhook acknowledgment)

    Behavior:
        - If call_status is "no-answer", "failed", or "busy":
          Triggers retry logic via handle_unanswered_calls()
        - If recording URL present and call is transfer:
          Saves recording URL to lead metadata
        - Otherwise: Logs the status and acknowledges

    Example:
        POST /twilio/callback/status
        Form data: CallSid=abc123&CallStatus=completed&RecordingUrl=https://...
    """
    return await handle_callback_status(request, provider)


# ---------------------------------------------------------------------------
# Transfer callbacks — unified under /{provider}/callback/transfer/{action}
# ---------------------------------------------------------------------------


@router.api_route(
    "/{provider}/callback/transfer/{action}",
    methods=["GET", "POST"],
)
async def transfer_callback(request: Request, provider: str, action: str):
    """
    Unified transfer callback endpoint.

    All transfer-related webhooks are routed here and dispatched
    by ``handle_call_transfer`` based on ``{provider}`` + ``{action}``.

    Supported (provider, action) combinations:
        - (exotel, dial-up)           — Fetch agent phone number to dial (GET)
        - (plivo, dial-up)            — Dial-back XML: agent → customer bridge (POST)
    """
    return await handle_call_transfer(request, provider, action)


@router.post("/twilio/callback/twiml-fallback")
async def twilio_twiml_fallback(request: Request):
    """
    Fallback TwiML endpoint for Twilio when Smart Router is unreachable.

    Twilio calls this via the ``fallback_url`` parameter when the primary webhook
    (Smart Router allocate) fails. Returns TwiML with static WebSocket URL so
    the call still works without pod isolation.

    Returns:
        TwiML XML with <Connect><Stream> pointing to static WebSocket URL
    """
    return await handle_twilio_twiml_fallback(request)

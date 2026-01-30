"""
Telephony provider callback handlers.

This module contains handlers for webhooks/callbacks from telephony providers
(Twilio, Exotel, Plivo, etc.) for call status updates and recording delivery.

Handlers:
- handle_callback_details_get() - GET callback for call details (Exotel)
- handle_callback_details_post() - POST callback for call details (Twilio)
- handle_callback_status() - POST callback for call status updates
- handle_plivo_answer() - POST answer webhook for Plivo (returns XML)
"""

from fastapi import BackgroundTasks, HTTPException, Request, Response
from starlette.responses import HTMLResponse

from app.ai.voice.agents.breeze_buddy.managers.calls import (
    handle_unanswered_calls,
    update_call_recording,
)
from app.core.config.dynamic import (
    BB_NOISE_CANCELLATION_ENABLED,
    BB_NOISE_CANCELLATION_LEVEL,
)
from app.core.config.static import APP_BASE_URL
from app.core.logger import logger


async def handle_callback_details_get(
    request: Request, provider: str, background_tasks: BackgroundTasks
) -> Response:
    """
    Handle GET callback for call details (typically from Exotel).

    This endpoint receives call recording URLs via query parameters.
    The recording URL is extracted and processed in the background.

    Args:
        request: FastAPI Request object with query parameters
        provider: Telephony provider name (e.g., "exotel")
        background_tasks: FastAPI BackgroundTasks for async processing

    Returns:
        200 OK response

    Raises:
        HTTPException: 404 if provider is not supported
    """
    query_params = dict(request.query_params)
    logger.info(f"Received call-details with {provider} query params: {query_params}")

    if provider.lower() != "exotel":
        raise HTTPException(
            status_code=404, detail="Feature not supported for this service provider"
        )

    call_sid = query_params.get("CallSid")
    provider_recording_url = query_params.get("Stream[RecordingUrl]")

    if provider_recording_url and call_sid:
        logger.info(
            f"Extracted recording_url: {provider_recording_url} and call_sid: {call_sid}"
        )
        background_tasks.add_task(
            update_call_recording, call_sid, provider_recording_url, provider.lower()
        )

    return Response(status_code=200)


async def handle_callback_details_post(
    request: Request, provider: str, background_tasks: BackgroundTasks
) -> Response:
    """
    Handle POST callback for call details (typically from Twilio).

    This endpoint receives call recording URLs via form data.
    The recording URL is extracted and processed in the background.

    Args:
        request: FastAPI Request object with form data
        provider: Telephony provider name (e.g., "twilio")
        background_tasks: FastAPI BackgroundTasks for async processing

    Returns:
        200 OK response

    Raises:
        HTTPException: 404 if provider is not supported
    """
    form = await request.form()
    logger.info(f"Received callback from {provider} with form data: {form}")

    if provider.lower() != "twilio":
        raise HTTPException(
            status_code=404, detail="Feature not supported for this service provider"
        )

    call_sid = form.get("CallSid")
    provider_recording_url = form.get("RecordingUrl")

    if provider_recording_url and call_sid:
        logger.info(
            f"Extracted recording_url: {provider_recording_url} and call_sid: {call_sid}"
        )
        background_tasks.add_task(
            update_call_recording, call_sid, provider_recording_url, provider.lower()
        )

    return Response(status_code=200)


async def handle_callback_status(request: Request, provider: str) -> Response:
    """
    Handle POST callback for call status updates.

    This endpoint receives call status updates from telephony providers
    (Twilio, Exotel). When a call fails (no-answer, failed, busy),
    it triggers retry logic.

    Supported providers:
    - Twilio: Uses "CallStatus" field
    - Exotel: Uses "Status" field

    Args:
        request: FastAPI Request object with form data
        provider: Telephony provider name (e.g., "twilio", "exotel")

    Returns:
        200 OK response
    """
    form = await request.form()
    logger.info(f"Received callback from {provider} with form data: {form}")

    call_sid = form.get("CallSid")
    call_status = None

    if provider.lower() == "twilio":
        call_status = form.get("CallStatus")
    elif provider.lower() == "exotel":
        call_status = form.get("Status")
    elif provider.lower() == "plivo":
        call_sid = form.get("CallUUID")
        call_status = form.get("CallStatus")

    logger.info(
        f"Extracted call_sid: {call_sid} and call_status: {call_status} from {provider}"
    )

    if call_status in ("no-answer", "failed", "busy"):
        logger.info(f"Call with SID {call_sid} failed with status: {call_status}")
        await handle_unanswered_calls(call_sid)

    return Response(status_code=200)


async def handle_plivo_answer(request: Request) -> HTMLResponse:
    """
    Handle POST answer webhook from Plivo.

    Plivo calls this endpoint when a call comes in.
    Returns XML to start streaming audio via WebSocket.

    The XML instructs Plivo to connect the call to the existing
    websocket endpoint for real-time audio streaming.

    Returns:
        XML Response with Stream element for WebSocket connection
    """
    form = await request.form()
    logger.info(f"Received Plivo answer webhook with form data: {form}")

    # Build WebSocket URL using existing endpoint pattern
    # Reuses the existing telephony websocket handler
    ws_path = "/agent/voice/breeze-buddy/plivo/callback/order-confirmation/v2"
    if APP_BASE_URL.startswith("https://"):
        ws_url = "wss://" + APP_BASE_URL[len("https://") :].rstrip("/") + ws_path
    elif APP_BASE_URL.startswith("http://"):
        ws_url = "ws://" + APP_BASE_URL[len("http://") :].rstrip("/") + ws_path
    else:
        # Default to wss:// if no scheme is present
        ws_url = "wss://" + APP_BASE_URL.rstrip("/") + ws_path

    # Generate XML response for Plivo
    noise_cancellation_enabled = await BB_NOISE_CANCELLATION_ENABLED()
    noise_cancellation_level = await BB_NOISE_CANCELLATION_LEVEL()
    noise_cancellation_attr = (
        f'noiseCancellation="{str(noise_cancellation_enabled).lower()}" '
        f'noiseCancellationLevel="{noise_cancellation_level}"'
        if noise_cancellation_enabled
        else ""
    )
    logger.info(f"Plivo Noise Cancellation Attributes: {noise_cancellation_attr}")

    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream {noise_cancellation_attr} bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">
        {ws_url}
    </Stream>
</Response>"""

    logger.info(f"Returning Plivo XML response with WebSocket URL: {ws_url}")

    return HTMLResponse(content=xml_content, media_type="application/xml")

"""
Telephony provider callback handlers.

This module contains handlers for webhooks/callbacks from telephony providers
(Twilio, Exotel, etc.) for call status updates and recording delivery.

Handlers:
- handle_callback_details_get() - GET callback for call details (Exotel)
- handle_callback_details_post() - POST callback for call details (Twilio)
- handle_callback_status() - POST callback for call status updates
"""

from fastapi import BackgroundTasks, HTTPException, Request, Response

from app.ai.voice.agents.breeze_buddy.managers.calls import (
    handle_unanswered_calls,
    update_call_recording,
)
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

    logger.info(
        f"Extracted call_sid: {call_sid} and call_status: {call_status} from {provider}"
    )

    if call_status in ("no-answer", "failed", "busy"):
        logger.info(f"Call with SID {call_sid} failed with status: {call_status}")
        await handle_unanswered_calls(call_sid)

    return Response(status_code=200)

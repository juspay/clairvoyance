"""
Telephony provider callback handlers.

This module contains handlers for webhooks/callbacks from telephony providers
(Twilio, Exotel, Plivo, etc.) for call status updates and recording delivery.

Handlers:
- handle_callback_details_get() - GET callback for call details (Exotel)
- handle_callback_details_post() - POST callback for call details (Twilio/Plivo)
- handle_callback_status() - POST callback for call status updates

Note: Plivo IVR is now handled agent-side (in-band over WebSocket) like Exotel,
so the handle_plivo_ivr_select() handler is no longer needed.
"""

import json

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
    Handle POST callback for call details (typically from Twilio or Plivo).

    This endpoint receives call recording URLs via form data.
    The recording URL is extracted and processed in the background.

    Args:
        request: FastAPI Request object with form data
        provider: Telephony provider name (e.g., "twilio", "plivo")
        background_tasks: FastAPI BackgroundTasks for async processing

    Returns:
        200 OK response

    Raises:
        HTTPException: 404 if provider is not supported
    """
    form = await request.form()
    logger.info(f"Received callback from {provider} with form data: {form}")

    provider_lower = provider.lower()
    if provider_lower not in ["twilio", "plivo"]:
        raise HTTPException(
            status_code=404, detail="Feature not supported for this service provider"
        )

    # Extract call_sid and recording_url based on provider
    call_sid = None
    provider_recording_url = None
    if provider_lower == "twilio":
        call_sid = form.get("CallSid")
        provider_recording_url = form.get("RecordingUrl")
    elif provider_lower == "plivo":
        # Plivo sends callback data as JSON string in 'response' field
        response_data = form.get("response")
        if response_data:
            try:
                response_str = (
                    str(response_data)
                    if not isinstance(response_data, str)
                    else response_data
                )
                plivo_data = json.loads(response_str)
                call_sid = plivo_data.get("call_uuid")
                provider_recording_url = plivo_data.get("record_url")
                logger.info(
                    f"Parsed Plivo response: call_uuid={call_sid}, record_url={provider_recording_url}"
                )
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse Plivo response JSON: {e}")
        else:
            # Fallback to direct form fields (older format)
            call_sid = form.get("call_uuid")
            provider_recording_url = form.get("record_url")

    if provider_recording_url and call_sid:
        # Ensure we have string values (form can return UploadFile)
        call_sid_str = str(call_sid) if not isinstance(call_sid, str) else call_sid
        recording_url_str = (
            str(provider_recording_url)
            if not isinstance(provider_recording_url, str)
            else provider_recording_url
        )
        logger.info(
            f"Extracted recording_url: {recording_url_str} and call_sid: {call_sid_str}"
        )
        background_tasks.add_task(
            update_call_recording, call_sid_str, recording_url_str, provider_lower
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
        # Convert to string for the handler
        if call_sid and isinstance(call_sid, str):
            await handle_unanswered_calls(call_sid)

    return Response(status_code=200)

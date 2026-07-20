"""
Telephony provider callback handlers.

Handlers:
- handle_callback_details_get()  - GET callback for call details (Exotel)
- handle_callback_details_post() - POST callback for call details (Twilio/Plivo)
- handle_call_transfer()         - Unified transfer callback
- handle_callback_status()       - POST callback for call status updates
- handle_twilio_twiml_fallback() - Fallback TwiML when Smart Router is down
"""

import json

from fastapi import BackgroundTasks, HTTPException, Request, Response
from starlette.responses import HTMLResponse
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from app.ai.voice.agents.breeze_buddy.managers.calls import (
    handle_unanswered_calls,
    update_call_recording,
)
from app.ai.voice.agents.breeze_buddy.services.agent_router.client import (
    safe_release_pod,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.exotel.exotel import (
    exotel_dial_text,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.plivo import (
    handle_mpc_transfer_webhook,
    plivo_dial_xml,
)
from app.ai.voice.agents.breeze_buddy.utils.hold_transfer import (
    publish_hold_transfer_result,
)
from app.ai.voice.agents.breeze_buddy.utils.warm_transfer import (
    get_transfer_flag,
)
from app.core.config.static import TWILIO_TEMPLATE_WEBSOCKET_URL
from app.core.logger import logger
from app.core.security.webhook_signature import verify_provider_webhook
from app.database.accessor import get_lead_by_call_id


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
    # Authenticate the provider webhook before trusting any of its data — an
    # unauthenticated caller must not be able to inject a recording URL that the
    # server then fetches with master provider credentials (PT-05).
    await verify_provider_webhook(request, provider)

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


async def handle_call_transfer(
    request: Request, provider: str, action: str
) -> Response:
    """Unified transfer callback — dispatches by provider + action."""
    # Authenticate before dispatching — an unauthenticated caller must not be
    # able to forge transfer callbacks (e.g. the Exotel dial-up path discloses
    # the live human-agent phone number) (PT-12).
    await verify_provider_webhook(request, provider)

    provider_lower = provider.lower()

    if action == "dial-up":
        return await _handle_transfer_dial_up(request, provider_lower)
    elif action == "mpc-transfer":
        return await _handle_mpc_transfer_status(request)
    elif action in ("conclude", "conference-end"):
        return HTMLResponse(
            content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
            media_type="application/xml",
        )
    else:
        logger.warning(f"[TRANSFER] Unknown callback: {provider}/{action}")
        return Response(
            status_code=404,
            content=f"Unknown transfer action: {provider}/{action}",
        )


async def _handle_mpc_transfer_status(request: Request) -> Response:
    """Handle Plivo MPC participant-state-changes webhook.

    Delegates to the Plivo service layer for all business logic.
    """
    params = {**dict(request.query_params), **dict(await request.form())}
    await handle_mpc_transfer_webhook(params)
    return Response(status_code=200)


async def _handle_transfer_dial_up(request: Request, provider: str) -> Response:
    """Return dial-target info so the provider can bridge the transfer.

    Exotel  → plain-text agent phone number.
    Plivo   → <Dial><Number> XML that bridges agent → customer.
    """
    params = {**dict(request.query_params), **dict(await request.form())}

    # Resolve the call SID used to look up the Redis transfer flag
    call_sid = params.get("CallSid") or params.get("customer_call_sid")
    if not call_sid:
        logger.error(f"[TRANSFER DIAL-UP] No call SID in {provider} request")
        return Response(status_code=404, content="Missing call SID")
    call_sid = str(call_sid)

    transfer_data = await get_transfer_flag(call_sid)
    if not transfer_data:
        # Plivo may re-fetch the answer_url after the call ends — return
        # a graceful hangup instead of 404 to avoid noisy error logs.
        if provider == "plivo":
            logger.info(
                f"[TRANSFER DIAL-UP] No transfer data for {call_sid} "
                "(likely already cleaned up) — returning <Hangup/>"
            )
            return HTMLResponse(
                content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
                media_type="application/xml",
            )
        return Response(status_code=404, content="Transfer not requested")

    if provider == "plivo":
        # Legacy immediate transfer: return <Dial><Number> XML that bridges
        # the customer's transferred leg to the agent. (The MPC dial-first
        # path uses the mpc-transfer callback instead, not this one.)
        return await plivo_dial_xml(transfer_data, call_sid, params)

    # Exotel — return agent phone number as plain text
    return await exotel_dial_text(transfer_data)


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
    # Authenticate before trusting the recording URL / call SID in the body
    # (PT-05).
    await verify_provider_webhook(request, provider)

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
    (Twilio, Exotel, Plivo). When a call fails (no-answer, failed, busy),
    it triggers retry logic.

    Also serves as a backup release mechanism — when a call ends, it notifies
    Smart Router to release the pod. This is idempotent and safe even if the
    WebSocket handler already released the pod.

    Supported providers:
    - Twilio: Uses "CallStatus" field
    - Exotel: Uses "Status" field
    - Plivo: Uses "CallStatus" field, "CallUUID" for call ID

    Args:
        request: FastAPI Request object with form data
        provider: Telephony provider name (e.g., "twilio", "exotel", "plivo")

    Returns:
        200 OK response
    """
    # Authenticate before acting on the status — a forged status callback could
    # release a live call's pod (DoS), force retries (toll fraud), or fabricate
    # outcomes (PT-12).
    await verify_provider_webhook(request, provider)

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

    # Terminal call statuses across all providers:
    # - completed, busy, failed, no-answer: universal (Twilio, Plivo, Exotel)
    # - canceled/cancelled: Twilio + Exotel (American/British spelling)
    # - cancel: Plivo (caller hung up before answer)
    # - timeout: Plivo (network/carrier timeout)
    ended_statuses = [
        "completed",
        "busy",
        "failed",
        "no-answer",
        "canceled",
        "cancelled",
        "cancel",
        "timeout",
    ]

    if call_sid and call_status:
        call_status = str(call_status)
        logger.info(
            f"Status callback: {call_status} for call {call_sid} from {provider}",
            extra={"call_sid": call_sid, "status": call_status, "provider": provider},
        )

        # Backup release: notify Smart Router when call ends.
        # Idempotent — safe even if WebSocket already released the pod.
        if call_status.lower() in ended_statuses:
            await safe_release_pod(
                call_sid=str(call_sid), reason=f"status_{call_status}"
            )

        # Handle failed calls for retry logic
        if call_status.lower() in (
            "no-answer",
            "failed",
            "busy",
            "timeout",
            "cancel",
            "canceled",
            "cancelled",
        ):
            logger.info(f"Call with SID {call_sid} failed with status: {call_status}")
            # Convert to string for the handler
            if isinstance(call_sid, str):
                # Hold-transfer: publish failure to inbound pod
                try:
                    lead = await get_lead_by_call_id(call_sid)
                    if lead and lead.payload:
                        pub_channel = lead.payload.get("_hold_transfer_pub_channel")
                        if pub_channel:
                            status_map = {
                                "no-answer": "no_answer",
                                "busy": "busy",
                                "failed": "failed",
                                "timeout": "no_answer",
                                "cancel": "no_answer",
                                "canceled": "no_answer",
                                "cancelled": "no_answer",
                            }
                            await publish_hold_transfer_result(
                                pub_channel,
                                {
                                    "status": status_map.get(
                                        call_status.lower(), call_status.lower()
                                    ),
                                    "summary": f"Outbound call {call_status.lower()}",
                                },
                            )
                            logger.info(
                                f"[hold_transfer] Published failure "
                                f"({call_status}) for call {call_sid}"
                            )
                except Exception as pub_error:
                    logger.error(
                        f"[hold_transfer] Failed to publish failure for "
                        f"call {call_sid}: {pub_error}"
                    )

                await handle_unanswered_calls(call_sid)

    return Response(status_code=200)


async def handle_twilio_twiml_fallback(request: Request) -> HTMLResponse:
    """
    Fallback TwiML endpoint for Twilio when Smart Router is unreachable.

    Twilio calls this via the ``fallback_url`` parameter when the primary ``url``
    (Smart Router webhook) fails (timeout, 5xx, connection refused). Returns
    TwiML with the static WebSocket URL — no pod isolation, but the call
    still works.

    This prevents calls from dropping silently when Smart Router is down.

    Returns:
        TwiML XML with <Connect><Stream> pointing to static WebSocket URL
    """
    # Authenticate — this is a Twilio webhook and must carry a valid signature.
    await verify_provider_webhook(request, "twilio")

    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    error_code = form.get("ErrorCode", "")
    error_url = form.get("ErrorUrl", "")

    logger.warning(
        "Twilio fallback triggered — Smart Router unreachable, using static WebSocket",
        extra={
            "call_sid": call_sid,
            "error_code": error_code,
            "error_url": error_url,
        },
    )

    # Use template WebSocket URL (v2 flow) as default fallback
    ws_url = TWILIO_TEMPLATE_WEBSOCKET_URL

    if not ws_url:
        logger.error(
            "No fallback WebSocket URL configured — call will drop",
            extra={"call_sid": call_sid},
        )
        return HTMLResponse(
            content=str(VoiceResponse()),
            media_type="application/xml",
        )

    # Build TwiML using Twilio SDK (consistent with twilio.py make_call)
    voice_response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=ws_url)
    connect.append(stream)
    voice_response.append(connect)

    return HTMLResponse(content=str(voice_response), media_type="application/xml")

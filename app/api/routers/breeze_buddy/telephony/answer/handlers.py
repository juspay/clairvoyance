"""
Call resolution and unified provider answer handler.

This module provides:
1. resolve_call_templates() - shared logic for resolving templates for both
   inbound and outbound calls, used by both Exotel and Plivo handlers.
2. handle_provider_answer() - unified handler for answering calls from any provider
   (Exotel returns JSON, Plivo returns XML).

Flow:

OUTBOUND (we call customer):
1. We create lead and initiate call via provider API
2. Customer answers -> provider calls /{provider}/answer endpoint
3. Look up lead -> return WebSocket URL/XML with template from lead

INBOUND (customer calls us):
1. Customer calls inbound number
2. Provider calls /{provider}/answer endpoint
3. Look up templates by outbound number
4. Single template: return direct WebSocket connection
5. Multiple templates: IVR mode (agent-side for both providers)
   - Pre-generate audio with our TTS
   - Store IVR config in Redis
   - Return WebSocket URL with ivr_mode=true
   - Agent handles IVR in-band over WebSocket
"""

import asyncio
import json
from html import escape as html_escape
from typing import Any, Dict
from urllib.parse import quote

from fastapi import HTTPException, Request, Response, status
from starlette.responses import HTMLResponse

from app.ai.voice.agents.breeze_buddy.agent.ivr import (
    IVR_CONFIG_CACHE_PREFIX,
    IVR_CONFIG_CACHE_TTL,
    prepare_goodbye_audio,
    prepare_ivr_menu_audio,
)
from app.ai.voice.agents.breeze_buddy.services.agent_router.client import (
    safe_allocate_pod,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.recording import (
    start_call_recording,
)
from app.core.config.dynamic import (
    BB_NOISE_CANCELLATION_ENABLED,
    BB_NOISE_CANCELLATION_LEVEL,
)
from app.core.config.static import APP_BASE_URL
from app.core.logger import logger
from app.database.accessor import get_lead_by_call_id
from app.database.accessor.breeze_buddy.outbound_number import (
    get_outbound_number_by_number,
)
from app.database.accessor.breeze_buddy.template import (
    get_all_templates_by_outbound_number_id,
    get_template_by_merchant,
)
from app.services.redis.client import get_redis_service


async def resolve_call_templates(
    call_sid: str, from_number: str, to_number: str
) -> Dict[str, Any]:
    """
    Shared call resolution logic used by both Exotel and Plivo handlers.

    1. Check if lead exists for call_sid (outbound detection)
    2. If outbound: look up template from lead
    3. If inbound: look up outbound_number by to_number, get all templates
    4. Build template_list with IVR descriptions
    5. Resolve voice_name and ivr_greeting from template configurations

    Args:
        call_sid: Unique call identifier (Exotel CallSid / Plivo CallUUID)
        from_number: Caller's phone number
        to_number: The number that was called

    Returns:
        Dict with keys:
            is_outbound: bool
            template_id: Optional[str]         (outbound: template id from lead)
            merchant_id: Optional[str]         (merchant ID for pod allocation)
            templates: List[TemplateModel]      (inbound: all matched templates)
            template_list: List[dict]           (inbound: [{id, name, description}])
            voice_name: str                     (IVR voice, default "sara")
            ivr_greeting: Optional[str]         (IVR greeting text)
            error: Optional[str]               (if lookup failed)
            error_status: Optional[int]        (HTTP status for error)
    """
    # Check if lead exists (outbound call)
    lead = await get_lead_by_call_id(call_sid)
    if lead:
        reseller = lead.reseller_id or lead.merchant_id
        if not reseller:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="reseller (or merchant for backward compatibility) is required",
            )
        # Outbound call - look up template using merchant info from lead
        logger.info(f"[Answer] Outbound call detected, lead: {lead.id}")
        template = await get_template_by_merchant(
            reseller_id=reseller,
            merchant_identifier=lead.merchant_identifier,
            name=lead.template,
        )
        if not template:
            logger.error(f"[Answer] Template not found for lead: {lead.id}")
            return {"error": "Template not found", "error_status": 404}

        return {
            "is_outbound": True,
            "template_id": str(template.id),
            "merchant_id": reseller,
        }

    # Inbound call - look up templates by outbound number
    logger.info("[Answer] Inbound call detected, looking up templates")

    if not to_number:
        logger.error("[Answer] No 'To' number in inbound call request")
        return {"error": "Missing To number", "error_status": 400}

    # Look up outbound number by phone number
    outbound_number = await get_outbound_number_by_number(to_number)
    if not outbound_number:
        logger.error(f"[Answer] No outbound number found for: {to_number}")
        return {"error": "Number not configured", "error_status": 404}

    # Get all templates for this outbound number
    templates = await get_all_templates_by_outbound_number_id(outbound_number.id)

    if not templates:
        logger.error(
            f"[Answer] No templates found for outbound_number_id: {outbound_number.id}"
        )
        return {"error": "No templates available", "error_status": 404}

    # Build template list for IVR (only id and name needed)
    template_list = [
        {
            "id": str(t.id),
            "name": t.name,
        }
        for t in templates
    ]

    # Resolve voice_name, ivr_greeting, and ivr_goodbye from template configurations
    voice_name = "sara"  # Default voice
    ivr_greeting = None
    ivr_goodbye = None

    first_template = templates[0]
    if first_template.configurations and first_template.configurations.tts_voice_name:
        voice_name = first_template.configurations.tts_voice_name.value

    for template in templates:
        if template.configurations:
            if not ivr_greeting and template.configurations.ivr_greeting:
                ivr_greeting = template.configurations.ivr_greeting
            if not ivr_goodbye and template.configurations.ivr_goodbye:
                ivr_goodbye = template.configurations.ivr_goodbye
            if ivr_greeting and ivr_goodbye:
                break

    # Warn if IVR mode (multiple templates) but no ivr_greeting configured
    if len(templates) > 1 and ivr_greeting is None:
        logger.warning(
            f"[Answer] IVR mode with {len(templates)} templates but no ivr_greeting configured "
            f"for outbound_number: {to_number}"
        )

    return {
        "is_outbound": False,
        "templates": templates,
        "template_list": template_list,
        "voice_name": voice_name,
        "ivr_greeting": ivr_greeting,
        "ivr_goodbye": ivr_goodbye,
        "merchant_id": first_template.reseller_id if first_template else None,
    }


# ---------------------------------------------------------------------------
# WebSocket URL builder
# ---------------------------------------------------------------------------


def _build_websocket_url(
    provider: str,
    template_id: str,
    from_number: str,
    to_number: str = "",
    ivr_mode: bool = False,
    pod_ws_url: str = "",
) -> str:
    """
    Build WebSocket URL with query params for any provider.

    When pod_ws_url is provided (from Smart Router allocation), it is used as
    the base instead of APP_BASE_URL. This routes the WebSocket connection to
    the specific pod allocated for this call (1-pod-1-call isolation).
    When pod_ws_url is empty, falls back to the standard shared URL.
    """
    if pod_ws_url:
        # Smart Router returned a pod-specific WebSocket URL.
        # Append query params so the pod's handler knows the template/caller.
        url = (
            f"{pod_ws_url}"
            f"?template_id={quote(template_id, safe='')}"
            f"&from_number={quote(from_number, safe='')}"
        )
    else:
        ws_base = APP_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")

        # Use callback/ws/v2 path for both providers - this matches the WebSocket endpoint
        # registered at /agent/voice/breeze-buddy/{provider}/callback/ws/v2 which handles
        # the bidirectional audio stream for voice conversations.
        url = (
            f"{ws_base}/agent/voice/breeze-buddy/{provider}/callback/ws/v2"
            f"?template_id={quote(template_id, safe='')}"
            f"&from_number={quote(from_number, safe='')}"
        )

    if to_number:
        url += f"&to_number={quote(to_number, safe='')}"

    if ivr_mode:
        url += "&ivr_mode=true"

    return url


# ---------------------------------------------------------------------------
# Plivo XML helper
# ---------------------------------------------------------------------------


async def _build_plivo_stream_xml(ws_url: str) -> str:
    """Build Plivo XML response with Stream element for WebSocket connection."""
    noise_cancellation_enabled = await BB_NOISE_CANCELLATION_ENABLED()
    noise_cancellation_level = await BB_NOISE_CANCELLATION_LEVEL()
    noise_cancellation_attr = (
        f'noiseCancellation="{str(noise_cancellation_enabled).lower()}" '
        f'noiseCancellationLevel="{noise_cancellation_level}"'
        if noise_cancellation_enabled
        else ""
    )

    if noise_cancellation_attr:
        logger.info(
            f"[Plivo] Noise Cancellation Attributes: "
            f'noiseCancellation="{str(noise_cancellation_enabled).lower()}" '
            f'noiseCancellationLevel="{noise_cancellation_level}"'
        )

    # Escape special XML characters in URL (primarily & -> &amp;)
    # Using html_escape with quote=False to avoid escaping quotes in the URL
    ws_url_escaped = html_escape(ws_url, quote=False)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream {noise_cancellation_attr} bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">
        {ws_url_escaped}
    </Stream>
</Response>"""


# ---------------------------------------------------------------------------
# Unified answer handler helpers
# ---------------------------------------------------------------------------


def _extract_call_params(params: dict, provider: str) -> tuple[str | None, str, str]:
    """Extract normalised (call_id, from_number, to_number) from request params."""
    if provider == "exotel":
        call_id = str(params.get("CallSid", "")) or None
        from_number = str(params.get("From") or params.get("CallFrom", "unknown"))
        to_number = str(
            params.get("To") or params.get("CallTo") or params.get("DialWhomNumber", "")
        )
    else:
        # Plivo (and future providers)
        call_id = str(params.get("CallUUID", "")) or None
        from_number = str(params.get("From", "unknown"))
        to_number = str(params.get("To", ""))

    return call_id, from_number, to_number


def _error_response(provider: str, message: str, status_code: int) -> Response:
    """Return an error response in the provider's expected format."""
    if provider == "exotel":
        return Response(
            content=json.dumps({"error": message}),
            media_type="application/json",
            status_code=status_code,
        )
    # Plivo expects XML - escape message for XML safety
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>{html_escape(message)}</Speak>
    <Hangup/>
</Response>"""
    return HTMLResponse(content=xml, media_type="application/xml")


def _build_json_response(ws_url: str) -> Response:
    """Build Exotel JSON response."""
    return Response(
        content=json.dumps({"url": ws_url}),
        media_type="application/json",
    )


async def _build_xml_response(ws_url: str) -> HTMLResponse:
    """Build Plivo XML response."""
    xml = await _build_plivo_stream_xml(ws_url)
    return HTMLResponse(content=xml, media_type="application/xml")


async def _build_provider_response(
    provider: str,
    result: dict,
    call_id: str,
    from_number: str,
    to_number: str,
) -> Response:
    """Build provider-specific response for a resolved call."""
    tag = f"Answer:{provider}"

    # Helper to build response based on provider
    async def make_response(ws_url: str) -> Response:
        if provider == "exotel":
            return _build_json_response(ws_url)
        return await _build_xml_response(ws_url)

    # ── Pod allocation (1-pod-1-call isolation) ──────────────────────────
    # Attempt to allocate a dedicated pod via Smart Router. This runs for
    # both inbound and outbound calls at answer-time (customer picked up).
    # If allocation succeeds, the returned ws_url routes the WebSocket
    # connection directly to that pod. If it fails (disabled, no capacity,
    # Smart Router down), pod_ws_url stays empty and we fall back to the
    # standard shared URL via _build_websocket_url().
    pod_ws_url = ""
    allocation = await safe_allocate_pod(
        call_sid=call_id,
        provider=provider,
        reseller_id=result.get("merchant_id") or result.get("reseller_id"),
        template="ws",
    )
    if allocation:
        pod_ws_url = allocation.ws_url
        logger.info(f"[{tag}] Pod allocated: {allocation.pod_name} for call {call_id}")

    # Outbound call - direct connection
    if result.get("is_outbound"):
        ws_url = _build_websocket_url(
            provider,
            result["template_id"],
            from_number,
            to_number,
            pod_ws_url=pod_ws_url,
        )
        logger.info(f"[{tag}] Outbound call - ws_url: {ws_url}")
        return await make_response(ws_url)

    template_list = result["template_list"]
    templates = result["templates"]

    # Single template - direct connection
    if len(templates) == 1:
        ws_url = _build_websocket_url(
            provider,
            template_list[0]["id"],
            from_number,
            to_number,
            pod_ws_url=pod_ws_url,
        )
        logger.info(f"[{tag}] Single template ({templates[0].name})")
        return await make_response(ws_url)

    # Multiple templates - IVR mode
    voice_name = result["voice_name"]
    ivr_greeting = result["ivr_greeting"]
    ivr_goodbye = result.get("ivr_goodbye")

    logger.info(f"[{tag}] IVR mode - {len(templates)} templates, voice={voice_name}")

    # Pre-generate IVR audio (checks cache first)
    await prepare_ivr_menu_audio(provider, voice_name, ivr_greeting)
    await prepare_goodbye_audio(provider, voice_name, ivr_goodbye)

    # Store IVR config in Redis
    ivr_config = {
        "options": template_list,
        "voice_name": voice_name,
        "ivr_greeting": ivr_greeting,
        "ivr_goodbye": ivr_goodbye,
    }
    redis = await get_redis_service()
    await redis.setex(
        f"{IVR_CONFIG_CACHE_PREFIX}{call_id}",
        json.dumps(ivr_config),
        IVR_CONFIG_CACHE_TTL,
    )

    ws_url = _build_websocket_url(
        provider,
        template_list[0]["id"],
        from_number,
        to_number,
        ivr_mode=True,
        pod_ws_url=pod_ws_url,
    )
    return await make_response(ws_url)


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


async def handle_provider_answer(request: Request, provider: str) -> Response:
    """
    Unified answer handler for all telephony providers (Exotel, Plivo).

    Returns:
        - Exotel: JSON ``{"url": "wss://..."}``
        - Plivo:  XML ``<Stream>``
    """
    tag = f"Answer:{provider}"

    # Extract call params
    if request.method == "GET":
        params = dict(request.query_params)
    else:
        params = dict(await request.form())

    call_id, from_number, to_number = _extract_call_params(params, provider)
    logger.info(f"[{tag}] call_id={call_id}, from={from_number}, to={to_number}")

    if not call_id:
        logger.error(f"[{tag}] Missing call ID")
        return _error_response(provider, "Missing call identifier", 400)

    # Plivo-specific: start recording
    if provider == "plivo":
        try:
            # Wait for call to be fully established before starting recording
            # Plivo requires 200-500ms delay between answer and record API
            # to ensure the call is fully connected internally
            logger.info(
                f"[{tag}] Waiting 500ms before starting recording for call: {call_id}"
            )
            await asyncio.sleep(0.5)  # 500ms delay (middle of 200-500ms range)

            recording_started = start_call_recording(call_id)
            if not recording_started:
                logger.error(f"[{tag}] Recording failed to start for call: {call_id}")
        except Exception as e:
            logger.error(
                f"[{tag}] Failed to start Plivo recording for call: {call_id} - {e}",
                exc_info=True,
            )

    # Resolve templates
    result = await resolve_call_templates(call_id, from_number, to_number)

    if "error" in result:
        logger.error(f"[{tag}] {result['error']}")
        error_msg = (
            result["error"]
            if provider == "exotel"
            else "Sorry, this number is not configured to receive calls. Goodbye."
        )
        return _error_response(provider, error_msg, result.get("error_status", 500))

    return await _build_provider_response(
        provider, result, call_id, from_number, to_number
    )

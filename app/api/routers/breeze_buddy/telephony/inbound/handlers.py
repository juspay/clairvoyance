"""
Inbound call resolution and unified provider answer handler.

This module provides:
1. resolve_inbound_templates() - shared logic for resolving templates for both
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
5. Multiple templates: IVR mode
   - Exotel: pre-generate audio, store config in Redis, agent handles IVR in-band
   - Plivo: return native <GetInput> XML, Plivo handles IVR via DTMF callback
"""

import base64
import json
from typing import Any, Dict
from urllib.parse import quote

from fastapi import Request, Response
from starlette.responses import HTMLResponse

from app.ai.voice.agents.breeze_buddy.agent.ivr import (
    IVR_CONFIG_CACHE_PREFIX,
    IVR_CONFIG_CACHE_TTL,
    prepare_goodbye_audio,
    prepare_ivr_menu_audio,
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

# Plivo IVR constants
PLIVO_IVR_MAX_ATTEMPTS = 3
PLIVO_IVR_INPUT_LENGTH = 1
PLIVO_IVR_TIMEOUT = 15  # seconds to wait for DTMF input


async def resolve_inbound_templates(
    call_sid: str, from_number: str, to_number: str
) -> Dict[str, Any]:
    """
    Shared inbound call resolution logic used by both Exotel and Plivo handlers.

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
        # Outbound call - look up template using merchant info from lead
        logger.info(f"[Inbound] Outbound call detected, lead: {lead.id}")
        template = await get_template_by_merchant(
            merchant_id=lead.merchant_id,
            shop_identifier=lead.shop_identifier,
            name=lead.template,
        )
        if not template:
            logger.error(f"[Inbound] Template not found for lead: {lead.id}")
            return {"error": "Template not found", "error_status": 404}

        return {
            "is_outbound": True,
            "template_id": str(template.id),
        }

    # Inbound call - look up templates by outbound number
    logger.info("[Inbound] Inbound call detected, looking up templates")

    if not to_number:
        logger.error("[Inbound] No 'To' number in inbound call request")
        return {"error": "Missing To number", "error_status": 400}

    # Look up outbound number by phone number
    outbound_number = await get_outbound_number_by_number(to_number)
    if not outbound_number:
        logger.error(f"[Inbound] No outbound number found for: {to_number}")
        return {"error": "Number not configured", "error_status": 404}

    # Get all templates for this outbound number
    templates = await get_all_templates_by_outbound_number_id(outbound_number.id)

    if not templates:
        logger.error(
            f"[Inbound] No templates found for outbound_number_id: {outbound_number.id}"
        )
        return {"error": "No templates available", "error_status": 404}

    # Build template list for IVR (use ivr_description from configurations, fallback to name)
    template_list = [
        {
            "id": str(t.id),
            "name": t.name,
            "description": (
                t.configurations.ivr_description
                if t.configurations and t.configurations.ivr_description
                else None
            ),
        }
        for t in templates
    ]

    # Resolve voice_name and ivr_greeting from template configurations
    voice_name = "sara"  # Default voice
    ivr_greeting = None

    first_template = templates[0]
    if first_template.configurations and first_template.configurations.tts_voice_name:
        voice_name = first_template.configurations.tts_voice_name.value

    for template in templates:
        if template.configurations and template.configurations.ivr_greeting:
            ivr_greeting = template.configurations.ivr_greeting
            break

    return {
        "is_outbound": False,
        "templates": templates,
        "template_list": template_list,
        "voice_name": voice_name,
        "ivr_greeting": ivr_greeting,
    }


# ---------------------------------------------------------------------------
# Provider-specific WebSocket URL builders
# ---------------------------------------------------------------------------


def _build_exotel_websocket_url(
    template_id: str,
    from_number: str,
    ivr_mode: bool = False,
) -> str:
    """Build Exotel WebSocket URL with query params."""
    ws_base = APP_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{ws_base}/agent/voice/breeze-buddy/exotel/callback/template/v2?template_id={template_id}&from_number={from_number}"

    if ivr_mode:
        url += "&ivr_mode=true"

    return url


def _build_plivo_websocket_url(
    template_id: str,
    from_number: str,
) -> str:
    """Build Plivo WebSocket URL with query params."""
    ws_base = APP_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    return (
        f"{ws_base}/agent/voice/breeze-buddy/plivo/callback/order-confirmation/v2"
        f"?template_id={template_id}&from_number={quote(from_number, safe='')}"
    )


# ---------------------------------------------------------------------------
# Provider-specific response builders (Plivo XML helpers)
# ---------------------------------------------------------------------------


async def build_plivo_stream_xml(ws_url: str) -> str:
    """Build Plivo XML response with Stream element for WebSocket connection."""
    noise_cancellation_enabled = await BB_NOISE_CANCELLATION_ENABLED()
    noise_cancellation_level = await BB_NOISE_CANCELLATION_LEVEL()
    noise_cancellation_attr = (
        f'noiseCancellation="{str(noise_cancellation_enabled).lower()}" '
        f'noiseCancellationLevel="{noise_cancellation_level}"'
        if noise_cancellation_enabled
        else ""
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream {noise_cancellation_attr} bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">
        {ws_url}
    </Stream>
</Response>"""


def build_plivo_ivr_xml(
    template_list: list,
    voice_name: str,
    ivr_greeting: str | None,
    from_number: str,
    to_number: str,
    attempt: int = 1,
) -> str:
    """Build Plivo XML with <GetInput> for IVR menu."""
    # Build menu text
    greeting = ivr_greeting or "Welcome"
    menu_options = ". ".join(
        f"Press {i + 1} for {t.get('description') or t['name']}"
        for i, t in enumerate(template_list)
    )
    menu_text = f"{greeting}. {menu_options}"

    # Encode template options as base64 JSON for passing through action URL
    options_json = json.dumps(template_list)
    options_b64 = base64.urlsafe_b64encode(options_json.encode()).decode()

    # Build action URL for receiving DTMF input
    action_url = (
        f"{APP_BASE_URL}/agent/voice/breeze-buddy/plivo/ivr-select"
        f"?attempt={attempt}"
        f"&options={options_b64}"
        f"&from_number={quote(from_number, safe='')}"
        f"&to_number={quote(to_number, safe='')}"
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <GetInput action="{action_url}" method="POST" inputType="dtmf" digitEndTimeout="{PLIVO_IVR_TIMEOUT}" numDigits="{PLIVO_IVR_INPUT_LENGTH}">
        <Speak>{menu_text}</Speak>
    </GetInput>
    <Speak>We didn't receive your input. Goodbye.</Speak>
</Response>"""


# ---------------------------------------------------------------------------
# Unified answer handler
# ---------------------------------------------------------------------------


def _extract_call_params(params: dict, provider: str) -> tuple[str | None, str, str]:
    """
    Extract normalised (call_id, from_number, to_number) from request params.

    Each provider sends different parameter names for the same fields.
    """
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
    # Plivo expects XML
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>{message}</Speak>
    <Hangup/>
</Response>"""
    return HTMLResponse(content=xml, media_type="application/xml")


async def _build_exotel_response(
    result: dict,
    call_id: str,
    from_number: str,
) -> Response:
    """Build the Exotel JSON response for a resolved call."""
    # Outbound call
    if result.get("is_outbound"):
        ws_url = _build_exotel_websocket_url(
            template_id=result["template_id"],
            from_number=from_number,
        )
        logger.info(f"[Answer:exotel] Outbound call, returning WebSocket URL: {ws_url}")
        return Response(
            content=json.dumps({"url": ws_url}),
            media_type="application/json",
        )

    template_list = result["template_list"]
    templates = result["templates"]

    if len(templates) == 1:
        # Single template – direct connection
        ws_url = _build_exotel_websocket_url(template_list[0]["id"], from_number)
        logger.info(
            f"[Answer:exotel] Single template ({templates[0].name}), returning WebSocket URL"
        )
    else:
        # Multiple templates – IVR mode
        voice_name = result["voice_name"]
        ivr_greeting = result["ivr_greeting"]

        logger.info(
            f"[Answer:exotel] Multiple templates ({len(templates)}), "
            f"generating IVR audio (voice={voice_name!r}, greeting={ivr_greeting!r})"
        )

        # Pre-generate IVR audio (checks cache first)
        await prepare_ivr_menu_audio(template_list, "exotel", voice_name, ivr_greeting)
        await prepare_goodbye_audio(template_list, "exotel", voice_name)

        # Store IVR config in Redis (keyed by call_id, 2 min TTL)
        ivr_config = {
            "options": template_list,
            "voice_name": voice_name,
            "ivr_greeting": ivr_greeting,
        }
        redis = await get_redis_service()
        await redis.setex(
            f"{IVR_CONFIG_CACHE_PREFIX}{call_id}",
            json.dumps(ivr_config),
            IVR_CONFIG_CACHE_TTL,
        )
        logger.info(
            f"[Answer:exotel] Stored IVR config in Redis for call_id: {call_id}"
        )

        ws_url = _build_exotel_websocket_url(
            template_id=template_list[0]["id"],
            from_number=from_number,
            ivr_mode=True,
        )
        logger.info(
            f"[Answer:exotel] IVR audio ready, returning WebSocket URL with {len(templates)} options"
        )

    return Response(
        content=json.dumps({"url": ws_url}),
        media_type="application/json",
    )


async def _build_plivo_response(
    result: dict,
    from_number: str,
    to_number: str,
) -> HTMLResponse:
    """Build the Plivo XML response for a resolved call."""
    # Outbound call
    if result.get("is_outbound"):
        ws_url = _build_plivo_websocket_url(
            template_id=result["template_id"],
            from_number=from_number,
        )
        logger.info(f"[Answer:plivo] Outbound call, returning Stream XML: {ws_url}")
        xml = await build_plivo_stream_xml(ws_url)
        return HTMLResponse(content=xml, media_type="application/xml")

    template_list = result["template_list"]
    templates = result["templates"]

    if len(templates) == 1:
        # Single template – direct connection via Stream
        ws_url = _build_plivo_websocket_url(
            template_id=template_list[0]["id"],
            from_number=from_number,
        )
        logger.info(
            f"[Answer:plivo] Single template ({templates[0].name}), returning Stream XML"
        )
        xml = await build_plivo_stream_xml(ws_url)
        return HTMLResponse(content=xml, media_type="application/xml")

    # Multiple templates – IVR mode using Plivo native <GetInput>
    voice_name = result["voice_name"]
    ivr_greeting = result["ivr_greeting"]

    logger.info(
        f"[Answer:plivo] Multiple templates ({len(templates)}), "
        f"returning IVR menu XML (voice={voice_name!r}, greeting={ivr_greeting!r})"
    )

    xml = build_plivo_ivr_xml(
        template_list=template_list,
        voice_name=voice_name,
        ivr_greeting=ivr_greeting,
        from_number=from_number,
        to_number=to_number,
        attempt=1,
    )
    return HTMLResponse(content=xml, media_type="application/xml")


async def handle_provider_answer(request: Request, provider: str) -> Response:
    """
    Unified answer handler for all telephony providers (Exotel, Plivo).

    Called when a call is answered. Resolves templates and returns either:
    - Exotel: JSON ``{"url": "wss://..."}``
    - Plivo:  XML ``<Stream>`` or ``<GetInput>``

    Args:
        request: The incoming webhook request from the provider.
        provider: Telephony provider name ("exotel" or "plivo").
    """
    tag = f"Answer:{provider}"

    # --- 1. Extract call params (provider-specific field names) ---
    if request.method == "GET":
        params = dict(request.query_params)
    else:
        params = dict(await request.form())

    call_id, from_number, to_number = _extract_call_params(params, provider)

    logger.info(
        f"[{tag}] Request - call_id: {call_id}, from: {from_number}, to: {to_number}"
    )

    # --- 2. Validate required call ID ---
    if not call_id:
        logger.error(f"[{tag}] No call ID in request")
        return _error_response(provider, "Missing call identifier", 400)

    # --- 3. Provider-specific pre-processing ---
    if provider == "plivo":
        try:
            start_call_recording(call_id)
        except Exception as e:
            logger.error(f"Failed to start Plivo recording: {e}", exc_info=True)

    # --- 4. Shared template resolution ---
    result = await resolve_inbound_templates(call_id, from_number, to_number)

    if "error" in result:
        logger.error(f"[{tag}] {result['error']}")
        error_msg = (
            result["error"]
            if provider == "exotel"
            else "Sorry, this number is not configured to receive calls. Goodbye."
        )
        return _error_response(provider, error_msg, result.get("error_status", 500))

    # --- 5. Build provider-specific response ---
    if provider == "exotel":
        return await _build_exotel_response(result, call_id, from_number)
    else:
        return await _build_plivo_response(result, from_number, to_number)

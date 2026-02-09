"""
Exotel Voicebot URL handler.

This module handles both inbound and outbound calls using Exotel's Voicebot applet.
The Voicebot applet is the single entry point - no Passthru applet needed.

Flow (Voicebot-only):

OUTBOUND (we call customer):
1. We create lead and initiate call via Exotel API
2. Customer answers → Voicebot applet calls /exotel/voicebot-url endpoint
3. Look up lead → return WebSocket URL with template from lead

INBOUND (customer calls us):
1. Customer calls inbound number
2. Voicebot applet calls /exotel/voicebot-url endpoint
3. Look up templates by outbound number
4. Pre-generate IVR audio if multiple templates
5. Store IVR config in Redis (keyed by call_sid)
6. Return JSON with WebSocket URL (includes ivr_mode flag)
7. Agent fetches IVR config from Redis and handles menu
"""

import json

from fastapi import Request, Response

from app.ai.voice.agents.breeze_buddy.agent.ivr import (
    IVR_CONFIG_CACHE_PREFIX,
    IVR_CONFIG_CACHE_TTL,
    prepare_goodbye_audio,
    prepare_ivr_menu_audio,
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


def _build_websocket_url(
    template_id: str,
    from_number: str,
    ivr_mode: bool = False,
) -> str:
    """
    Build WebSocket URL with query params.

    Args:
        template_id: Template ID to use (or first template for IVR)
        from_number: Caller's phone number
        ivr_mode: If True, agent should handle IVR menu (config fetched from Redis)
    """
    # Convert https:// to wss:// for WebSocket
    ws_base = APP_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{ws_base}/agent/voice/breeze-buddy/exotel/callback/template/v2?template_id={template_id}&from_number={from_number}&sample-rate=16000"

    if ivr_mode:
        url += "&ivr_mode=true"

    return url


async def handle_voicebot_url(request: Request) -> Response:
    """
    Handle Voicebot applet request for WebSocket URL (both inbound and outbound).

    This is the single entry point for all Exotel calls (no Passthru needed).

    For outbound: Looks up lead and returns WebSocket URL with lead's template.
    For inbound: Looks up templates by outbound number and returns WebSocket URL.

    Query params from Exotel:
        CallSid: Unique identifier for the call
        CallFrom/From: The caller's phone number
        CallTo/To: The number that was called

    Returns:
        JSON response: {"url": "wss://..."}
    """
    # Support both GET (query params) and POST (form data)
    if request.method == "GET":
        params = dict(request.query_params)
    else:
        params = dict(await request.form())

    # Ensure string types (form data can return UploadFile for file fields)
    call_sid = str(params.get("CallSid", "")) or None
    from_number = str(params.get("From") or params.get("CallFrom", "unknown"))
    to_number = str(
        params.get("To") or params.get("CallTo") or params.get("DialWhomNumber", "")
    )

    logger.info(
        f"[Voicebot] URL request - CallSid: {call_sid}, From: {from_number}, To: {to_number}"
    )

    if not call_sid:
        logger.error("[Voicebot] No 'CallSid' in request")
        return Response(
            content='{"error": "Missing CallSid"}',
            media_type="application/json",
            status_code=400,
        )

    # Check if lead exists (outbound call)
    lead = await get_lead_by_call_id(call_sid)
    if lead:
        # Outbound call - look up template using merchant info from lead
        logger.info(f"[Voicebot] Outbound call detected, lead: {lead.id}")
        template = await get_template_by_merchant(
            merchant_id=lead.merchant_id,
            shop_identifier=lead.shop_identifier,
            name=lead.template,
        )
        if not template:
            logger.error(f"[Voicebot] Template not found for lead: {lead.id}")
            return Response(
                content='{"error": "Template not found"}',
                media_type="application/json",
                status_code=404,
            )

        ws_url = _build_websocket_url(
            template_id=str(template.id),
            from_number=from_number,
        )
        logger.info(f"[Voicebot] Outbound call, returning WebSocket URL: {ws_url}")
        return Response(
            content=json.dumps({"url": ws_url}),
            media_type="application/json",
        )

    # Inbound call - look up templates by outbound number
    logger.info("[Voicebot] Inbound call detected, looking up templates")

    if not to_number:
        logger.error("[Voicebot] No 'To' number in inbound call request")
        return Response(
            content='{"error": "Missing To number"}',
            media_type="application/json",
            status_code=400,
        )

    # Look up outbound number by phone number
    outbound_number = await get_outbound_number_by_number(to_number)
    if not outbound_number:
        logger.error(f"[Voicebot] No outbound number found for: {to_number}")
        return Response(
            content='{"error": "Number not configured"}',
            media_type="application/json",
            status_code=404,
        )

    # Get all templates for this outbound number
    templates = await get_all_templates_by_outbound_number_id(outbound_number.id)

    if not templates:
        logger.error(
            f"[Voicebot] No templates found for outbound_number_id: {outbound_number.id}"
        )
        return Response(
            content='{"error": "No templates available"}',
            media_type="application/json",
            status_code=404,
        )

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

    if len(templates) == 1:
        # Single template - direct connection
        ws_url = _build_websocket_url(template_list[0]["id"], from_number)
        logger.info(
            f"[Voicebot] Single template ({templates[0].name}), returning WebSocket URL"
        )
    else:
        # Multiple templates - IVR mode
        # Use first template's voice, check all templates for ivr_greeting (use first found)
        voice_name = "sara"  # Default voice
        ivr_greeting = None

        # Get voice from first template (use .value to get string from enum)
        first_template = templates[0]
        if (
            first_template.configurations
            and first_template.configurations.tts_voice_name
        ):
            voice_name = first_template.configurations.tts_voice_name.value

        # Check all templates for ivr_greeting, use first one found
        for template in templates:
            if template.configurations and template.configurations.ivr_greeting:
                ivr_greeting = template.configurations.ivr_greeting
                break

        logger.info(
            f"[Voicebot] Multiple templates ({len(templates)}), "
            f"generating IVR audio (voice={voice_name!r}, greeting={ivr_greeting!r})"
        )

        # Pre-generate IVR audio (checks cache first)
        await prepare_ivr_menu_audio(template_list, "exotel", voice_name, ivr_greeting)
        await prepare_goodbye_audio(template_list, "exotel", voice_name)

        # Store IVR config in Redis (keyed by call_sid, 2 min TTL)
        # WebSocket agent will fetch this config instead of parsing URL params
        ivr_config = {
            "options": template_list,
            "voice_name": voice_name,
            "ivr_greeting": ivr_greeting,
        }
        redis = await get_redis_service()
        await redis.setex(
            f"{IVR_CONFIG_CACHE_PREFIX}{call_sid}",
            json.dumps(ivr_config),
            IVR_CONFIG_CACHE_TTL,
        )
        logger.info(f"[Voicebot] Stored IVR config in Redis for call_sid: {call_sid}")

        # Build WebSocket URL with IVR mode flag only (config is in Redis)
        ws_url = _build_websocket_url(
            template_id=template_list[0]["id"],
            from_number=from_number,
            ivr_mode=True,
        )

        logger.info(
            f"[Voicebot] IVR audio ready, returning WebSocket URL with {len(templates)} options"
        )

    return Response(
        content=json.dumps({"url": ws_url}),
        media_type="application/json",
    )

"""Utility functions for voice agents."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from fastapi import WebSocket

from app.ai.voice.agents.breeze_buddy.agent.websocket import send_message
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.utils.common import (
    prepare_initial_greeting_payload,
    track_error,
)
from app.core.logger import logger
from app.schemas.breeze_buddy.core import LeadCallTracker


@dataclass
class GreetingResult:
    """Result of sending initial greeting."""

    source: Optional[str]  # "template_static", "lead_dynamic", or None
    text: Optional[str]  # The resolved greeting text for LLM context


async def send_initial_greeting(
    ws: WebSocket,
    stream_sid: str,
    lead: LeadCallTracker,
    template: TemplateModel,
    provider: Optional[str],
    errors: Optional[List[Dict[str, Any]]] = None,
) -> GreetingResult:
    """Send initial greeting audio for telephony calls.

    Args:
        ws: WebSocket connection
        stream_sid: Stream SID for the call
        lead: Lead data
        template: Template model
        provider: Telephony provider (twilio/exotel/plivo)
        errors: Optional errors list to track failures

    Returns:
        GreetingResult with source and resolved greeting text
    """
    try:
        greeting_result = await prepare_initial_greeting_payload(
            lead=lead,
            template=template,
            provider=provider,
        )
        if greeting_result is None:
            logger.warning("Failed to prepare greeting payload, skipping initial audio")
            track_error(
                errors, "Failed to prepare greeting payload, skipping initial audio"
            )
            return GreetingResult(source=None, text=None)

        if greeting_result.get("greeting_source") == "disabled":
            logger.info("Initial audio intentionally skipped (ringing sound disabled)")
            return GreetingResult(source="disabled", text=None)

        greeting_source = greeting_result["greeting_source"]
        greeting_text = greeting_result.get("greeting_text")

        # Build provider-specific WebSocket message
        # Plivo uses different event name and message structure than Twilio/Exotel
        provider_str = (
            provider.lower()
            if provider and hasattr(provider, "lower")
            else str(provider or "").lower()
        )

        if provider_str == "plivo":
            # Plivo bidirectional streaming uses playAudio event
            media_message = {
                "event": "playAudio",
                "streamId": stream_sid,
                "media": {
                    "contentType": "audio/x-mulaw",
                    "sampleRate": 8000,
                    "payload": greeting_result["payload"],
                },
            }
        else:
            # Twilio/Exotel use media event with streamSid
            media_message = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": greeting_result["payload"]},
            }

        success = await send_message(ws=ws, message=media_message)
        if success:
            logger.info(
                f"Successfully sent initial greeting for streamSid: {stream_sid} (provider: {provider_str})"
            )
            return GreetingResult(source=greeting_source, text=greeting_text)
        else:
            logger.warning("Failed to send initial greeting message via WebSocket")
            track_error(errors, "Failed to send initial greeting message via WebSocket")
        return GreetingResult(source=None, text=None)

    except Exception as e:
        logger.error(f"Failed to send initial greeting: {e}")
        track_error(errors, f"Failed to send initial greeting: {e}")
        return GreetingResult(source=None, text=None)


async def end_call_with_errors(
    lead: LeadCallTracker,
    errors: List[Dict[str, Any]],
    completion_function: Callable,
    transport_type: str,
    call_sid: Optional[str] = None,
    outcome: str = "UNKNOWN",
    call_ended_by: str = "system",
) -> Optional[LeadCallTracker]:
    """
    End call early due to Agent initialization/setup errors.

    Uses completion_function to properly handle cleanup (number release, DB update).

    Args:
        lead: Lead call tracker record
        errors: List of error dictionaries with timestamps
        completion_function: Call completion handler (usually handle_call_completion)
        transport_type: Transport mode ("daily" or telephony provider name)
        call_sid: Telephony call SID (None for Daily mode)
        outcome: Call outcome (default: "UNKNOWN" prevents retries)
        call_ended_by: Who/what ended call (default: "system")

    Returns:
        Updated LeadCallTracker if successful, None if failed
    """
    if not lead:
        logger.info("Cannot end call with errors: no lead provided")
        return None

    if lead.metaData is None:
        lead.metaData = {}

    lead.metaData["errors"] = errors
    lead.metaData["call_ended_by"] = call_ended_by

    # Calculate call_id based on transport type
    # - Daily mode: Uses lead.id (UUID) because no telephony call_sid exists
    # - Telephony mode: Uses call_sid for proper lead lookup in completion function
    is_daily_mode = transport_type == "daily"
    call_id = lead.id if is_daily_mode else (call_sid or lead.id)

    try:
        logger.info(
            f"Ending call with errors: call_id={call_id}, outcome={outcome}, "
            f"ended_by={call_ended_by}, errors={len(errors)}"
        )
        updated_lead = await completion_function(
            call_id=call_id,
            outcome=outcome,
            call_end_time=datetime.now(timezone.utc),
            meta_data=lead.metaData,
        )
        logger.info(f"Successfully ended call with errors: {call_id}")
        return updated_lead
    except Exception as e:
        logger.error(
            f"Failed to end call with errors for {call_id}: {e}", exc_info=True
        )
        return None

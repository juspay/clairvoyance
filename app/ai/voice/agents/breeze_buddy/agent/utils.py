"""Utility functions for voice agents."""

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


async def send_initial_greeting(
    ws: WebSocket,
    stream_sid: str,
    lead: LeadCallTracker,
    template: TemplateModel,
    provider: Optional[str],
    errors: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    """Send initial greeting audio for telephony calls.

    Args:
        ws: WebSocket connection
        stream_sid: Stream SID for the call
        lead: Lead data
        template: Template model
        provider: Telephony provider (twilio/exotel/plivo)
        errors: Optional errors list to track failures

    Returns:
        Greeting source if successful, None otherwise
    """
    try:
        greeting_result = await prepare_initial_greeting_payload(
            lead=lead,
            template=template,
            provider=provider,
        )
        if not greeting_result:
            logger.warning("Failed to prepare greeting payload, skipping initial audio")
            track_error(
                errors, "Failed to prepare greeting payload, skipping initial audio"
            )
            return None

        greeting_source = greeting_result["greeting_source"]
        media_message = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": greeting_result["payload"]},
        }
        success = await send_message(ws=ws, message=media_message)
        if success:
            logger.info(
                f"Successfully sent initial greeting for streamSid: {stream_sid}"
            )
            return greeting_source
        else:
            logger.warning("Failed to send initial greeting message via WebSocket")
            track_error(errors, "Failed to send initial greeting message via WebSocket")
        return None

    except Exception as e:
        logger.error(f"Failed to send initial greeting: {e}")
        track_error(errors, f"Failed to send initial greeting: {e}")
        return None


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

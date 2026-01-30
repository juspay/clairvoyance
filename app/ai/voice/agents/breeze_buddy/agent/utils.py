"""Utility functions for voice agents."""

from typing import Optional

from fastapi import WebSocket

from app.ai.voice.agents.breeze_buddy.agent.websocket import send_message
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.utils.common import (
    prepare_initial_greeting_payload,
)
from app.core.logger import logger
from app.schemas.breeze_buddy.core import LeadCallTracker


async def send_initial_greeting(
    ws: WebSocket,
    stream_sid: str,
    lead: LeadCallTracker,
    template: TemplateModel,
    provider: Optional[str],
) -> Optional[str]:
    """Send initial greeting audio for telephony calls.

    Args:
        ws: WebSocket connection
        stream_sid: Stream SID for the call
        lead: Lead data
        template: Template model
        provider: Telephony provider (twilio/exotel/plivo)

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
        return None

    except Exception as e:
        logger.error(f"Failed to send initial greeting: {e}")
        return None

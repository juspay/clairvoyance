"""Utility functions for voice agents."""

import audioop
import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from fastapi import WebSocket
from pipecat.frames.frames import OutputAudioRawFrame
from pipecat.pipeline.task import PipelineTask

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.utils.common import (
    prepare_initial_greeting_payload,
    track_error,
)
from app.ai.voice.agents.breeze_buddy.utils.transport.websockets import send_message
from app.core.logger import logger
from app.schemas.breeze_buddy.core import LeadCallTracker
from app.services.redis.client import get_redis_service

# Daily output runs at PipelineParams default (24 kHz, 16-bit, mono); the
# telephony greeting cache stores mulaw 8 kHz, so we transcode at retrieval.
DAILY_OUTPUT_SAMPLE_RATE = 24000
TELEPHONY_GREETING_SAMPLE_RATE = 8000
PCM_SAMPLE_WIDTH_BYTES = 2  # 16-bit linear PCM


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
        if greeting_result and greeting_result.get("skipped"):
            # Intentional no-playback (dial_tone disabled for a realtime LLM that
            # speaks first). Not a failure — skip media send + error tracking.
            return GreetingResult(source=None, text=None)
        if not greeting_result:
            logger.warning("Failed to prepare greeting payload, skipping initial audio")
            track_error(
                errors, "Failed to prepare greeting payload, skipping initial audio"
            )
            return GreetingResult(source=None, text=None)

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


def _transcode_mulaw_to_daily_pcm(mulaw_bytes: bytes) -> bytes:
    """Transcode mulaw 8 kHz mono → PCM 16-bit 24 kHz mono for Daily playback.

    Two-step stdlib path: ``audioop.ulaw2lin`` decodes mulaw to 16-bit linear
    PCM at the source rate (8 kHz); ``audioop.ratecv`` upsamples 8→24 kHz with
    linear interpolation. Speech-quality is fine for a 3–5 s greeting; the
    floor was already telephony-quality mulaw.
    """
    pcm_8k = audioop.ulaw2lin(mulaw_bytes, PCM_SAMPLE_WIDTH_BYTES)
    pcm_daily, _state = audioop.ratecv(
        pcm_8k,
        PCM_SAMPLE_WIDTH_BYTES,
        1,  # mono
        TELEPHONY_GREETING_SAMPLE_RATE,
        DAILY_OUTPUT_SAMPLE_RATE,
        None,
    )
    return pcm_daily


async def send_initial_greeting_daily(
    task: PipelineTask,
    lead: LeadCallTracker,
    template: TemplateModel,
    errors: Optional[List[Dict[str, Any]]] = None,
) -> GreetingResult:
    """Send initial greeting audio for Daily-mode calls.

    Reuses the telephony Redis greeting cache (mulaw 8 kHz), transcodes to PCM
    24 kHz to match the Daily transport's output sample rate, and queues an
    ``OutputAudioRawFrame`` through the pipeline for playback.

    The cache keys are identical to telephony, so the dispatch worker's
    pre-synthesis (which runs for outbound) populates the cache for both
    transport types. Inbound Daily calls fall back to lazy synthesis via
    ``prepare_and_store_initial_greeting``
    in the agent setup, same pattern as inbound telephony.

    Args:
        task: Pipeline task to queue the audio frame on
        lead: Lead data
        template: Template model
        errors: Optional errors list to track failures

    Returns:
        GreetingResult with source and resolved greeting text. ``source`` is
        ``None`` when no cached audio is available (falls through to LLM-speaks-first).
    """
    try:
        redis = await get_redis_service()
        mulaw_data: Optional[bytes] = None
        greeting_source: Optional[str] = None
        greeting_text: Optional[str] = None

        # 1. Static greeting (persistent template-level cache): the TTS
        # path's synthesized audio or the Gemini Live pre-generated opening
        # line — same key, raw base64 mulaw, kept fresh by PUT/DELETE
        # invalidation. Greetings are static, so the playback text is the
        # template's initial_greeting itself.
        template_greeting = await redis.get(f"greeting:template:{template.id}")
        if template_greeting:
            mulaw_data = base64.b64decode(template_greeting)
            greeting_source = "template_static"
            if template.configurations and template.configurations.initial_greeting:
                greeting_text = template.configurations.initial_greeting

        # 2. Dynamic greeting (per-lead cache, deleted after retrieval)
        if not mulaw_data:
            lead_greeting_key = f"greeting:{lead.id}"
            lead_greeting_data = await redis.get(lead_greeting_key)
            if lead_greeting_data:
                greeting_source = "lead_dynamic"
                try:
                    greeting_obj = json.loads(lead_greeting_data)
                    mulaw_data = base64.b64decode(greeting_obj["audio"])
                    greeting_text = greeting_obj.get("text")
                except (json.JSONDecodeError, KeyError):
                    # Legacy format: raw base64 mulaw, no JSON wrapper
                    mulaw_data = base64.b64decode(lead_greeting_data)
                await redis.delete(lead_greeting_key)
                logger.info(f"Deleted dynamic greeting from Redis for lead {lead.id}")

        if not mulaw_data:
            logger.info("No greeting audio cached for Daily call; LLM will speak first")
            return GreetingResult(source=None, text=None)

        pcm_daily = _transcode_mulaw_to_daily_pcm(mulaw_data)

        await task.queue_frame(
            OutputAudioRawFrame(
                audio=pcm_daily,
                sample_rate=DAILY_OUTPUT_SAMPLE_RATE,
                num_channels=1,
            )
        )
        logger.info(
            f"Queued Daily initial greeting (source={greeting_source}, "
            f"{len(pcm_daily)} bytes PCM at {DAILY_OUTPUT_SAMPLE_RATE} Hz)"
        )
        return GreetingResult(source=greeting_source, text=greeting_text)

    except Exception as e:
        logger.error(f"Failed to send Daily initial greeting: {e}", exc_info=True)
        track_error(errors, f"Failed to send Daily initial greeting: {e}")
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

"""
IVR menu handling for inbound calls.

Handles:
- IVR config caching (Redis) - stores options, voice, greeting per call_sid
- IVR audio generation and caching (Redis) - stores pre-generated TTS audio
- Menu playback with retry logic (3 attempts, 15 sec timeout)
- DTMF input detection
- Template ID extraction from call data (with IVR support)

Similar pattern to prepare_initial_greeting_payload in utils/common.py
"""

import asyncio
import audioop
import base64
import hashlib
import json
import uuid
from typing import Dict, List, Optional, Tuple

from fastapi import WebSocket

from app.ai.voice.agents.breeze_buddy.tts import generate_audio
from app.ai.voice.agents.breeze_buddy.utils.transport.websockets import (
    close_websocket_safely,
    send_message,
)
from app.core.logger import logger
from app.services.redis.client import get_redis_service

# Constants
IVR_MAX_ATTEMPTS = 3
IVR_TIMEOUT_SECONDS = 15  # Time to wait for DTMF after audio finishes (~6 sec audio + ~9 sec response time)
IVR_AUDIO_CACHE_PREFIX = "ivr_audio:menu:"
IVR_GOODBYE_CACHE_PREFIX = "ivr_audio:goodbye:"
IVR_AUDIO_CACHE_TTL = 86400  # 24 hours
IVR_CONFIG_CACHE_PREFIX = (
    "ivr_config:"  # Per-call IVR config (options, voice, greeting, goodbye)
)
IVR_CONFIG_CACHE_TTL = 120  # 2 minutes - enough time for WebSocket to connect
IVR_DEFAULT_GOODBYE = "We didn't receive your input. Goodbye."


async def get_template_id_from_call(
    ws: WebSocket,
    stream_sid: str,
    call_sid: str,
    call_data: dict,
    provider: str,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract template_id from call data, handling IVR mode if needed.

    This function handles:
    1. Extracting custom_params from correct location (Exotel vs Twilio)
    2. For Plivo: Extracting template_id from WebSocket URL query params
    3. Running IVR menu if ivr_mode is enabled
    4. Validating template_id UUID format
    5. Closing WebSocket on errors

    Args:
        ws: WebSocket connection
        stream_sid: Stream ID for sending audio
        call_sid: Call ID used as Redis key for IVR config
        call_data: Parsed call data from telephony provider
        provider: "twilio", "exotel", or "plivo"

    Returns:
        Tuple of (template_id, error_reason):
        - (template_id, None) on success
        - (None, None) if no template_id provided (valid for non-IVR inbound)
        - (None, error_reason) on error (WebSocket already closed)
    """
    # Extract custom_params (Exotel: root level, Twilio: inside "start")
    start_data = call_data.get("start", {})
    custom_params = call_data.get("custom_parameters") or start_data.get(
        "custom_parameters", {}
    )

    template_id = custom_params.get("template_id")
    ivr_mode = custom_params.get("ivr_mode")

    # For Plivo: Extract template_id from WebSocket URL query params
    # Plivo's WebSocket call_data only contains stream_id and call_id,
    # so we pass template_id, from_number, to_number via URL query params
    if provider.lower() == "plivo" and not template_id:
        url_query_params = dict(ws.query_params)
        template_id = url_query_params.get("template_id")
        ivr_mode = url_query_params.get("ivr_mode")
        if template_id:
            logger.info(f"[Plivo] Extracted template_id from URL: {template_id}")

    # Handle IVR mode (multiple templates available)
    if ivr_mode == "true":
        logger.info("[IVR] Agent-side IVR mode enabled")
        selected_template_id = await _run_ivr_menu(
            ws=ws,
            stream_sid=stream_sid,
            call_sid=call_sid,
            provider=provider,
        )

        if not selected_template_id:
            logger.error("[IVR] No valid template selected, ending call")
            # WebSocket already closed by IVR menu after goodbye audio
            return None, "IVR failed - no template selected"

        logger.info(f"[IVR] Template selected via DTMF: {selected_template_id}")
        return selected_template_id, None

    # Validate template_id UUID format (if provided)
    if template_id:
        try:
            uuid.UUID(template_id)
        except ValueError:
            logger.error(f"Invalid template_id format: {template_id}")
            await close_websocket_safely(
                ws, code=4000, reason="Invalid template_id format"
            )
            return None, "Invalid template_id format"

    return template_id, None


async def _run_ivr_menu(
    ws: WebSocket,
    stream_sid: str,
    call_sid: str,
    provider: str,
) -> Optional[str]:
    """
    Fetch IVR config from Redis and run IVR menu.

    This function handles:
    1. Fetching IVR config from Redis (stored by HTTP handler)
    2. Running the IVR menu and returning the selected template ID

    Args:
        ws: WebSocket connection
        stream_sid: Stream ID for sending audio
        call_sid: Call ID used as Redis key for IVR config
        provider: "twilio" or "exotel"

    Returns:
        Selected template_id or None if IVR failed/no selection
    """
    try:
        # Fetch IVR config from Redis (stored by HTTP handler)
        redis = await get_redis_service()
        ivr_config_json = await redis.get(f"{IVR_CONFIG_CACHE_PREFIX}{call_sid}")

        if not ivr_config_json:
            logger.error(f"[IVR] Config not found in Redis for call_sid: {call_sid}")
            return None

        ivr_config = json.loads(ivr_config_json)
        ivr_options = ivr_config["options"]
        voice_name = ivr_config.get("voice_name", "sara")
        ivr_greeting = ivr_config.get("ivr_greeting")
        ivr_goodbye = ivr_config.get("ivr_goodbye")

        logger.info(
            f"[IVR] Fetched config from Redis - "
            f"{len(ivr_options)} options: {[o['name'] for o in ivr_options]}, "
            f"voice={voice_name!r}, greeting={ivr_greeting!r}, goodbye={ivr_goodbye!r}"
        )

        # Handle IVR menu - speak options and wait for DTMF
        selected_template_id = await handle_ivr_menu(
            ws=ws,
            stream_sid=stream_sid,
            ivr_options=ivr_options,
            provider=provider,
            voice_name=voice_name,
            ivr_greeting=ivr_greeting,
            ivr_goodbye=ivr_goodbye,
        )

        return selected_template_id

    except json.JSONDecodeError as e:
        logger.error(f"[IVR] Failed to parse IVR config from Redis: {e}")
        return None
    except Exception as e:
        logger.error(f"[IVR] Failed to setup IVR: {e}")
        return None


async def handle_ivr_menu(
    ws: WebSocket,
    stream_sid: str,
    ivr_options: List[Dict[str, Optional[str]]],
    provider: str,
    voice_name: str = "sara",
    ivr_greeting: Optional[str] = None,
    ivr_goodbye: Optional[str] = None,
) -> Optional[str]:
    """
    Handle IVR menu with retry logic.

    Plays menu audio, waits for DTMF input.
    Retries 3 times with 5 second timeout each.
    Returns selected template_id or None.

    Args:
        ws: WebSocket connection
        stream_sid: Stream ID for sending audio
        ivr_options: List of {"id": str, "name": str}
        provider: "twilio" or "exotel"
        voice_name: TTS voice to use (default "sara")
        ivr_greeting: Full IVR audio text including greeting and menu options
        ivr_goodbye: Goodbye message when no input received (default: English fallback)

    Returns:
        Selected template_id or None if no valid selection
    """
    if not ivr_options:
        logger.error("[IVR] No options provided")
        return None

    # Get cached/generated audio once (reuse for retries)
    menu_audio = await prepare_ivr_menu_audio(provider, voice_name, ivr_greeting)
    goodbye_audio = await prepare_goodbye_audio(provider, voice_name, ivr_goodbye)

    if not menu_audio:
        logger.error("[IVR] Failed to prepare menu audio")
        return None

    for attempt in range(1, IVR_MAX_ATTEMPTS + 1):
        logger.info(f"[IVR] Attempt {attempt}/{IVR_MAX_ATTEMPTS}")

        # Send menu audio
        await _send_audio(ws, stream_sid, menu_audio, provider)

        # Wait for DTMF with timeout
        try:
            selected_id = await asyncio.wait_for(
                _wait_for_valid_dtmf(ws, ivr_options, stream_sid, provider),
                timeout=IVR_TIMEOUT_SECONDS,
            )
            if selected_id:
                logger.info(f"[IVR] Template selected: {selected_id}")
                return selected_id

        except asyncio.TimeoutError:
            logger.info(f"[IVR] Timeout on attempt {attempt}")
            # Continue to next attempt (menu will replay)

    # All attempts exhausted - say goodbye and close
    logger.info("[IVR] Max attempts reached, sending goodbye")
    if goodbye_audio:
        await _send_audio(ws, stream_sid, goodbye_audio, provider)
        # Wait for goodbye audio to play (~3 seconds) before closing
        await asyncio.sleep(3)

    # Close the WebSocket to end the call
    logger.info("[IVR] Closing WebSocket to end call")
    await ws.close()

    return None


async def prepare_ivr_menu_audio(
    provider: str,
    voice_name: str = "sara",
    ivr_greeting: Optional[str] = None,
) -> Optional[bytes]:
    """
    Prepare IVR menu audio - from cache or generate new.

    Similar pattern to prepare_initial_greeting_payload.
    Stores as mulaw in Redis, converts to provider format on retrieval.

    Args:
        provider: "twilio", "exotel", or "plivo"
        voice_name: TTS voice to use (default "sara")
        ivr_greeting: Full IVR audio text including greeting and menu options

    Returns:
        Audio bytes ready to send, or None if failed
    """
    # ivr_greeting is required - it contains the full menu text defined by the user
    if not ivr_greeting:
        logger.error(
            "[IVR] No ivr_greeting provided - cannot generate menu audio. "
            "Please define ivr_greeting in template configurations with the full menu text."
        )
        return None

    try:
        redis = await get_redis_service()

        # Generate cache key from ivr_greeting text and voice
        # MD5 hash ensures constant key size regardless of input length
        cache_components = f"greeting:{ivr_greeting}|voice:{voice_name}"
        cache_key = f"{IVR_AUDIO_CACHE_PREFIX}{hashlib.md5(cache_components.encode()).hexdigest()}"

        # Check cache first
        cached_audio = await redis.get(cache_key)
        if cached_audio:
            logger.info("[IVR] Cache HIT - using cached menu audio")
            mulaw_data = base64.b64decode(cached_audio)
        else:
            # Generate TTS from user-defined ivr_greeting
            logger.info(
                f"[IVR] Cache MISS - generating menu audio for: {ivr_greeting!r}"
            )

            mulaw_data = await _generate_tts_audio_mulaw(ivr_greeting, voice_name)

            if mulaw_data:
                # Cache for future calls
                await redis.setex(
                    cache_key,
                    base64.b64encode(mulaw_data).decode("utf-8"),
                    IVR_AUDIO_CACHE_TTL,
                )
                logger.info("[IVR] Menu audio cached")
            else:
                logger.error("[IVR] Failed to generate menu audio")
                return None

        # Convert format based on provider (same as greeting)
        return _convert_audio_for_provider(mulaw_data, provider)

    except Exception as e:
        logger.error(f"[IVR] Failed to prepare menu audio: {e}")
        return None


async def prepare_goodbye_audio(
    provider: str,
    voice_name: str = "sara",
    ivr_goodbye: Optional[str] = None,
) -> Optional[bytes]:
    """
    Get cached goodbye audio or generate it.

    Args:
        provider: "twilio" or "exotel"
        voice_name: TTS voice to use (default "sara")
        ivr_goodbye: Custom goodbye message (default: English fallback)

    Returns:
        Audio bytes ready to send, or None if failed
    """
    try:
        redis = await get_redis_service()

        # Use provided goodbye message or fall back to English default
        goodbye_text = ivr_goodbye or IVR_DEFAULT_GOODBYE

        # Generate cache key from goodbye text and voice name
        # MD5 hash ensures constant key size regardless of input length
        cache_components = f"goodbye:{goodbye_text}|voice:{voice_name}"
        cache_key = f"{IVR_GOODBYE_CACHE_PREFIX}{hashlib.md5(cache_components.encode()).hexdigest()}"

        cached = await redis.get(cache_key)
        if cached:
            logger.info("[IVR] Using cached goodbye audio")
            mulaw_data = base64.b64decode(cached)
        else:
            # Generate
            logger.info(f"[IVR] Generating goodbye audio: {goodbye_text!r}")
            mulaw_data = await _generate_tts_audio_mulaw(goodbye_text, voice_name)

            if mulaw_data:
                await redis.setex(
                    cache_key,
                    base64.b64encode(mulaw_data).decode("utf-8"),
                    IVR_AUDIO_CACHE_TTL,
                )
            else:
                return None

        return _convert_audio_for_provider(mulaw_data, provider)

    except Exception as e:
        logger.error(f"[IVR] Failed to prepare goodbye audio: {e}")
        return None


async def _generate_tts_audio_mulaw(
    text: str, voice_name: str = "sara"
) -> Optional[bytes]:
    """
    Generate TTS audio using existing TTS abstraction.

    Args:
        text: Text to synthesize
        voice_name: TTS voice to use (default "sara")

    Returns:
        Audio bytes in mulaw format, or None if failed
    """
    try:
        mulaw_data = await generate_audio(text=text, voice_name=voice_name)
        logger.info(f"[IVR] Generated TTS audio: {len(mulaw_data)} bytes mulaw")
        return mulaw_data
    except Exception as e:
        logger.error(f"[IVR] TTS generation failed: {e}")
        return None


def _convert_audio_for_provider(mulaw_data: bytes, provider: str) -> bytes:
    """
    Convert mulaw audio to provider-specific format.

    Same logic as prepare_initial_greeting_payload.

    Args:
        mulaw_data: Audio in mulaw format
        provider: "twilio", "exotel", or "plivo"

    Returns:
        Audio bytes in provider-specific format
    """
    provider_str = (
        provider.lower() if hasattr(provider, "lower") else str(provider).lower()
    )

    if provider_str in ("twilio", "plivo"):
        # Twilio and Plivo expect mulaw
        return mulaw_data
    else:
        # Exotel expects raw PCM 16-bit
        pcm_data = audioop.ulaw2lin(mulaw_data, 2)
        return pcm_data


async def _send_audio(
    ws: WebSocket, stream_sid: str, audio_bytes: bytes, provider: str = "exotel"
):
    """
    Send audio to caller via WebSocket.

    Args:
        ws: WebSocket connection
        stream_sid: Stream ID for the media message
        audio_bytes: Audio bytes to send
        provider: Telephony provider ("twilio", "exotel", or "plivo")
    """
    payload = base64.b64encode(audio_bytes).decode("utf-8")

    # Build provider-specific media message format
    # Plivo uses different event name and message structure than Twilio/Exotel
    provider_str = (
        provider.lower() if hasattr(provider, "lower") else str(provider).lower()
    )

    if provider_str == "plivo":
        # Plivo bidirectional streaming uses playAudio event
        media_message = {
            "event": "playAudio",
            "streamId": stream_sid,
            "media": {
                "contentType": "audio/x-mulaw",
                "sampleRate": 8000,
                "payload": payload,
            },
        }
    else:
        # Twilio/Exotel use media event with streamSid
        media_message = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": payload},
        }

    success = await send_message(ws=ws, message=media_message)
    if success:
        logger.info(f"[IVR] Sent audio ({len(audio_bytes)} bytes) via {provider_str}")
    else:
        logger.error("[IVR] Failed to send audio")


async def _wait_for_valid_dtmf(
    ws: WebSocket,
    ivr_options: List[Dict[str, Optional[str]]],
    stream_sid: str,
    provider: str = "exotel",
) -> Optional[str]:
    """
    Wait for a valid DTMF digit and interrupt audio playback.

    Only returns when a valid digit is pressed or call stops.
    Invalid digits are logged but ignored (keeps waiting).
    Sends clearAudio command to interrupt playback when valid digit received.

    Args:
        ws: WebSocket connection
        ivr_options: List of {"id": str, "name": str}
        stream_sid: Stream ID for clearAudio command
        provider: Telephony provider for clearAudio support

    Returns:
        Selected template_id or None if call stopped
    """
    provider_str = (
        provider.lower() if hasattr(provider, "lower") else str(provider).lower()
    )

    async for message in ws.iter_text():
        try:
            data = json.loads(message)
            event = data.get("event")

            if event == "dtmf":
                digit = data.get("dtmf", {}).get("digit")
                if digit:
                    logger.info(f"[IVR] Received DTMF: {digit}")
                    try:
                        index = int(digit) - 1
                        if 0 <= index < len(ivr_options):
                            # Valid digit - interrupt audio playback
                            logger.info(
                                f"[IVR] Valid digit {digit} - interrupting audio"
                            )
                            # Send clearAudio to stop provider's audio playback
                            if provider_str == "plivo":
                                clear_message = {
                                    "event": "clearAudio",
                                    "streamId": stream_sid,
                                }
                                await send_message(ws=ws, message=clear_message)
                                logger.info("[IVR] Sent clearAudio command to Plivo")

                            return ivr_options[index]["id"]
                        else:
                            # Invalid digit - do NOT interrupt audio
                            logger.warning(
                                f"[IVR] Invalid digit {digit}, only {len(ivr_options)} options available"
                            )
                    except ValueError:
                        logger.warning(f"[IVR] Non-numeric digit: {digit}")

            elif event == "stop":
                logger.info("[IVR] Call stopped")
                return None

        except json.JSONDecodeError:
            continue

    return None

import audioop
import base64
import json
import os
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import soundfile
from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer
from pipecat.frames.frames import OutputAudioRawFrame
from pydub import AudioSegment

from app.core.config.static import ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY
from app.core.logger import logger
from app.core.security.sha import calculate_hmac_sha256
from app.services.redis.client import get_redis_service


def track_error(
    errors: Optional[List[Dict[str, Any]]],
    error_msg: str,
) -> None:
    """Track error with timestamp. Does NOT log - caller is responsible for logging.

    Args:
        errors: Error list to append to (None is safely ignored)
        error_msg: Error message to track in database (truncated to 2000 chars)

    Example:
        logger.error(f"Template loading failed: {str(e)}")
        track_error(self.errors, f"Template loading failed: {str(e)}")
    """
    try:
        # Defensive: if errors list is None, just return
        if errors is None:
            return

        MAX_ERROR_LENGTH = 2000

        # Truncate long error messages to prevent DB overload
        if len(error_msg) > MAX_ERROR_LENGTH:
            error_msg = error_msg[:MAX_ERROR_LENGTH] + "... [truncated]"

        errors.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": error_msg,
            }
        )
    except Exception as e:
        # Never let error tracking crash the call
        logger.warning(f"Failed to track error: {e}")


def greeting_has_variables(greeting_text: str) -> bool:
    """
    Check if greeting text contains variable placeholders like {customer_name}.

    Args:
        greeting_text: The greeting text to check

    Returns:
        True if greeting contains variable placeholders, False otherwise
    """
    # Match patterns like {variable_name} or {customer_name}
    pattern = r"\{([^}]+)\}"
    return bool(re.search(pattern, greeting_text))


def convert_to_mulaw(audio_data: bytes, input_format: str = "raw") -> bytes:
    """
    Convert audio data to mulaw format compatible with Twilio (8kHz, mono).

    Args:
        audio_data: Raw audio bytes
        input_format: Input audio format ("raw", "mp3", "ulaw", etc.)

    Returns:
        Audio bytes in mulaw format
    """
    try:
        # ElevenLabs ulaw_8000 format - already in the correct format!
        if input_format == "ulaw":
            return audio_data

        # For raw PCM, use audioop directly for resampling and conversion
        if input_format != "mp3":
            # Pad audio data to ensure whole number of frames (2 bytes per frame for 16-bit mono)
            frame_size = 2  # 16-bit = 2 bytes
            if len(audio_data) % frame_size != 0:
                padding_needed = frame_size - (len(audio_data) % frame_size)
                audio_data = audio_data + b"\x00" * padding_needed

            downsampled, _ = audioop.ratecv(audio_data, 2, 1, 16000, 8000, None)
            # Convert to mulaw (sample_width=2 for 16-bit PCM)
            mulaw_data = audioop.lin2ulaw(downsampled, 2)
            return mulaw_data

        # For MP3 format from ElevenLabs
        audio_segment = AudioSegment.from_file(BytesIO(audio_data), format="mp3")
        audio_segment = (
            audio_segment.set_frame_rate(8000).set_channels(1).set_sample_width(2)
        )
        pcm_data: bytes = audio_segment.raw_data  # type: ignore[assignment]
        mulaw_data = audioop.lin2ulaw(pcm_data, 2)
        return mulaw_data

    except Exception as e:
        logger.error(f"Error converting audio to mulaw: {e}")
        raise


async def send_webhook_with_retry(
    session, url: str, data: dict, max_retries: int = 3
) -> bool:
    """
    Sends a webhook with retry logic up to max_retries attempts.
    Returns True if any attempt succeeds (status 200), False otherwise.

    Args:
        session: aiohttp session
        url: webhook URL
        data: payload data
        max_retries: maximum number of attempts (default 3)

    Returns:
        bool: True if successful, False if all attempts failed
    """
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    signature = calculate_hmac_sha256(payload, ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY)
    headers = {"Content-Type": "application/json"}
    if signature:
        headers["checksum"] = signature

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Webhook attempt {attempt}/{max_retries} to {url}")
            async with session.post(url, json=data, headers=headers) as response:
                if response.status == 200:
                    logger.info(f"Webhook succeeded on attempt {attempt}")
                    return True
                else:
                    response_text = await response.text()
                    logger.warning(
                        f"Webhook attempt {attempt} failed. Status: {response.status}, Body: {response_text}"
                    )
        except Exception as e:
            logger.error(f"Webhook attempt {attempt} error: {e}", exc_info=True)

        # Don't sleep after the last attempt
        if attempt < max_retries:
            logger.info(f"Retrying webhook (attempt {attempt + 1}/{max_retries})...")

    logger.error(f"All {max_retries} webhook attempts failed for {url}")
    return False


def load_audio(audio_path) -> OutputAudioRawFrame | None:
    """
    Load and process the audio file.

    Returns:
        OutputAudioRawFrame: Processed audio frame ready for transport, or None if loading failed
    """

    if os.path.exists(audio_path):
        try:
            # Load audio file using pydub and convert to transport format
            audio_segment = AudioSegment.from_wav(audio_path)

            # Convert to 8000 Hz sample rate, mono channel, 16-bit PCM to match transport config
            audio_segment = (
                audio_segment.set_frame_rate(8000).set_channels(1).set_sample_width(2)
            )

            # Get raw audio data
            raw_audio_data: bytes = audio_segment.raw_data  # type: ignore[assignment]

            # Create OutputAudioRawFrame with correct parameters
            audio_sound = OutputAudioRawFrame(
                audio=raw_audio_data,
                sample_rate=8000,  # Match transport sample rate
                num_channels=1,  # Match transport channels (mono)
            )
            logger.info(
                "Loaded and resampled one moment audio from WAV file successfully"
            )
            return audio_sound
        except Exception as e:
            logger.warning(f"Failed to load and process WAV audio: {e}")
    else:
        logger.warning(f"One moment audio file not found: {audio_path}")

    return None


def validate_payload(
    payload: Dict[str, Any], expected_schema: Dict[str, Any]
) -> Tuple[bool, List[str]]:
    """
    Validate a payload against an expected schema.

    Args:
        payload: The actual payload data to validate
        expected_schema: A dictionary mapping field names to their expected types.
                        Supports simple types (string) and nested schemas (dict with type/properties/items)

                        Examples:
                        Simple: {"order_id": "string", "total_price": "number"}

                        Nested object: {
                            "customer": {
                                "type": "object",
                                "properties": {
                                    "name": "string",
                                    "age": "number"
                                }
                            }
                        }

                        Nested array: {
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "product_name": "string",
                                        "quantity": "number"
                                    }
                                }
                            }
                        }

    Returns:
        Tuple of (is_valid, errors) where:
        - is_valid: Boolean indicating if payload is valid
        - errors: List of error messages (empty if valid)
    """
    errors = []

    if not expected_schema:
        # No schema to validate against
        return True, []

    # Check for missing required fields and validate types
    for field_name, field_schema in expected_schema.items():
        # Check if field is marked as required
        is_optional = False
        if isinstance(field_schema, dict):
            is_optional = field_schema.get("optional", False)

        # Validate required fields
        if not is_optional and field_name not in payload:
            errors.append(f"Missing required field: '{field_name}'")
            continue

        # Validate field type if field exists in payload
        if field_name in payload:
            value = payload[field_name]
            field_errors = _validate_field(value, field_schema, field_name)
            errors.extend(field_errors)

    is_valid = len(errors) == 0
    return is_valid, errors


def _validate_field(value: Any, field_schema: Any, field_path: str) -> List[str]:
    """
    Validate a single field against its schema.

    Args:
        value: The value to validate
        field_schema: Either a simple type string (e.g., "string") or a dict with nested schema
        field_path: The path to this field (for error messages)

    Returns:
        List of error messages (empty if valid)
    """
    errors = []

    # If field_schema is a simple string type
    if isinstance(field_schema, str):
        if not _validate_type(value, field_schema):
            errors.append(
                f"Invalid type for field '{field_path}': expected {field_schema}, got {type(value).__name__}"
            )
        return errors

    # If field_schema is a dict (nested schema)
    if isinstance(field_schema, dict):
        schema_type = field_schema.get("type")

        if not schema_type:
            # If no type is specified, treat the entire dict as the schema
            errors.append(
                f"Invalid schema for field '{field_path}': missing 'type' key"
            )
            return errors

        # Validate the base type first
        if not _validate_type(value, schema_type):
            errors.append(
                f"Invalid type for field '{field_path}': expected {schema_type}, got {type(value).__name__}"
            )
            return errors

        # Validate nested properties for objects
        if schema_type == "object" and isinstance(value, dict):
            properties = field_schema.get("properties", {})
            for prop_name, prop_schema in properties.items():
                prop_path = f"{field_path}.{prop_name}"

                # Check if property is required
                is_prop_optional = False
                if isinstance(prop_schema, dict):
                    is_prop_optional = prop_schema.get("optional", False)
                    logger.debug(f"Property '{prop_name}' optional: {is_prop_optional}")

                if not is_prop_optional and prop_name not in value:
                    errors.append(f"Missing required property: '{prop_path}'")
                    continue

                # Validate property if it exists
                if prop_name in value:
                    prop_errors = _validate_field(
                        value[prop_name], prop_schema, prop_path
                    )
                    errors.extend(prop_errors)

        # Validate array items
        elif schema_type == "array" and isinstance(value, list):
            items_schema = field_schema.get("items")
            if items_schema:
                for idx, item in enumerate(value):
                    item_path = f"{field_path}[{idx}]"
                    item_errors = _validate_field(item, items_schema, item_path)
                    errors.extend(item_errors)

    return errors


def _validate_type(value: Any, expected_type: str) -> bool:
    """
    Validate that a value matches the expected type.

    Supported types:
    - string: str
    - number: int or float
    - boolean: bool
    - array: list
    - object: dict
    - null: None

    Args:
        value: The value to validate
        expected_type: The expected type name

    Returns:
        Boolean indicating if the value matches the expected type
    """
    type_validators = {
        "string": lambda v: isinstance(v, str),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
        "null": lambda v: v is None,
        "any": lambda v: True,  # Accept any type
    }

    validator = type_validators.get(expected_type.lower())

    if validator is None:
        # Unknown type, accept the value
        return True

    # Allow null values for optional fields
    if value is None:
        return True

    return validator(value)


def get_validation_error_message(errors: List[str]) -> str:
    """
    Format validation errors into a single error message.

    Args:
        errors: List of validation error messages

    Returns:
        Formatted error message string
    """
    if not errors:
        return ""

    if len(errors) == 1:
        return f"Payload validation failed: {errors[0]}"

    error_list = "\n- ".join(errors)
    return f"Payload validation failed with {len(errors)} errors:\n- {error_list}"


def create_background_sound_mixer(template) -> Optional[SoundfileMixer]:
    """
    Create a background sound mixer from template configuration.

    Args:
        template: Template object with configurations for background sound

    Returns:
        SoundfileMixer instance if successfully configured, None otherwise
    """
    # Constant path for background sound audio files
    BACKGROUND_SOUND_AUDIO_PATH = "app/ai/voice/agents/breeze_buddy/static/audio"

    # Check if background sound is enabled in template
    if (
        not template
        or not template.configurations
        or not template.configurations.enable_background_sound
    ):
        logger.info("Background sound mixer disabled (not configured in template)")
        return None

    background_sound_file = template.configurations.background_sound_file
    background_sound_volume = template.configurations.background_sound_volume

    if not background_sound_file:
        logger.warning("Background sound enabled but no file specified in template")
        return None

    # Mapping from background sound file identifier to actual filename with extension
    background_sound_file_map = {"office-ambience": "office-ambience.mp3"}

    # Convert enum to string value if needed
    background_sound_key = (
        background_sound_file.value
        if hasattr(background_sound_file, "value")
        else background_sound_file
    )

    # Map to actual filename with extension
    background_sound_filename = background_sound_file_map.get(
        background_sound_key, background_sound_key
    )

    # Construct full path to audio file
    full_audio_path = os.path.join(
        BACKGROUND_SOUND_AUDIO_PATH, background_sound_filename
    )

    if not os.path.exists(full_audio_path):
        logger.warning(
            f"Background sound enabled but file not found: {full_audio_path}"
        )
        return None

    # Validate that the audio file is mono before creating the mixer
    try:
        audio_info = soundfile.info(full_audio_path)
        if audio_info.channels != 1:
            logger.warning(
                f"Background sound file is not mono (channels={audio_info.channels}). "
                f"Skipping mixer creation to avoid runtime failures: {background_sound_filename}"
            )
            return None

        # Create and return the mixer
        audio_out_mixer = SoundfileMixer(
            sound_files={"background": full_audio_path},
            default_sound="background",
            volume=background_sound_volume,
        )
        logger.info(
            f"Background sound mixer enabled: file={background_sound_filename}, "
            f"volume={background_sound_volume}"
        )
        return audio_out_mixer

    except Exception as e:
        logger.warning(
            f"Failed to read audio file info for {background_sound_filename}: {e}"
        )
        return None


async def prepare_initial_greeting_payload(
    lead,
    template,
    provider,
) -> Optional[Dict[str, Any]]:
    """
    Prepare initial greeting audio payload for calls.

    Checks for greeting audio in Redis (template static or lead dynamic),
    falls back to dial tone if none found, and returns the media payload.

    Args:
        lead: Lead object containing lead information
        template: Template object containing template configuration
        provider: Call provider (enum or string "twilio" or "exotel")

    Returns:
        Dictionary with "payload" (base64 encoded audio) and "greeting_source",
        or None if preparation failed
    """
    try:
        logger.info("Preparing initial greeting audio payload.")

        # Convert provider to lowercase string for comparison
        provider_str = (
            provider.lower() if hasattr(provider, "lower") else str(provider).lower()
        )

        mulaw_data = None
        greeting_source = None

        if lead:
            redis = await get_redis_service()

            # 1. First check for template audio (static greeting - persistent)
            if template:
                template_audio_key = f"greeting:template:{template.id}"
                template_greeting = await redis.get(template_audio_key)
                if template_greeting:
                    logger.info("Using static greeting audio from template")
                    mulaw_data = base64.b64decode(template_greeting)
                    greeting_source = "template_static"
                    # Don't delete template audio - it's persistent

            # 2. Then check for lead audio (dynamic greeting - temporary)
            if not mulaw_data:
                lead_audio_key = f"greeting:{lead.id}"
                lead_greeting = await redis.get(lead_audio_key)
                if lead_greeting:
                    logger.info("Using dynamic greeting audio for lead")
                    mulaw_data = base64.b64decode(lead_greeting)
                    greeting_source = "lead_dynamic"

                    # Delete from Redis after retrieving (cleanup)
                    await redis.delete(lead_audio_key)
                    logger.info(
                        f"Deleted dynamic greeting from Redis for lead {lead.id}"
                    )

        # 3. Fall back to dial tone if no greeting audio found
        if not mulaw_data:
            logger.info("No greeting audio found, using dial tone")
            wav_file_path = (
                "app/ai/voice/agents/breeze_buddy/static/audio/dial-tone.wav"
            )

            # Load and convert audio
            audio = AudioSegment.from_wav(wav_file_path)
            audio = audio.set_frame_rate(8000).set_channels(1).set_sample_width(2)
            pcm_data: bytes = audio.raw_data  # type: ignore[assignment]
            mulaw_data = audioop.lin2ulaw(pcm_data, 2)

        logger.info(f"Prepared initial greeting audio from source: {greeting_source}")

        # Convert audio format based on provider
        # Twilio expects mulaw, Exotel expects raw PCM (16-bit, 8kHz, mono)
        if provider_str == "twilio":
            # mulaw_data is already in correct format for Twilio
            audio_to_send = mulaw_data
            logger.info("Audio prepared as mulaw for Twilio")
        else:
            try:
                # Convert mulaw to 16-bit linear PCM
                pcm_data = audioop.ulaw2lin(mulaw_data, 2)
                audio_to_send = pcm_data
                logger.info("Converted audio to raw PCM for Exotel")
            except Exception as e:
                logger.error(f"Failed to convert mulaw to PCM for Exotel: {e}")
                return None

        # Encode payload
        payload = base64.b64encode(audio_to_send).decode("utf-8")

        return {"payload": payload, "greeting_source": greeting_source}

    except Exception as e:
        logger.error(f"Failed to prepare initial greeting payload: {e}")
        return None

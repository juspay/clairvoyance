"""Utility functions for call manager."""

import base64
import json
from typing import Optional

from app.ai.voice.agents.breeze_buddy.template.transformation_function import (
    TEMPLATE_FUNCTION_REGISTRY,
)
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel, TTSVoiceName
from app.ai.voice.agents.breeze_buddy.tts import generate_audio
from app.ai.voice.agents.breeze_buddy.utils.common import greeting_has_variables
from app.core.logger import logger
from app.services.redis.client import get_redis_service


async def prepare_and_store_initial_greeting(
    lead_id: str,
    payload: dict,
    template: TemplateModel,
) -> Optional[str]:
    """
    Synthesize and store initial greeting audio in Redis.

    Handles both static (template-level) and dynamic (lead-level) greetings.
    Static greetings are cached per template, dynamic greetings are per-lead.

    Args:
        lead_id: The lead ID for dynamic greeting key
        payload: Lead payload for variable substitution
        template: Template with initial greeting configuration

    Returns:
        The resolved greeting text if successful, None otherwise
    """
    if (
        not template
        or not template.configurations
        or not template.configurations.initial_greeting
    ):
        return None

    try:
        initial_greeting = template.configurations.initial_greeting
        redis = await get_redis_service()

        # Check if greeting has variables
        has_variables = greeting_has_variables(initial_greeting)

        if has_variables:
            # Dynamic greeting - check if already exists for this lead
            lead_greeting_key = f"greeting:{lead_id}"
            existing_data = await redis.get(lead_greeting_key)
            if existing_data:
                # Parse JSON object containing both audio and text
                try:
                    greeting_data = json.loads(existing_data)
                    logger.info(f"Using existing dynamic greeting for lead {lead_id}")
                    return greeting_data.get("text")
                except json.JSONDecodeError:
                    # Legacy format - just audio bytes, no text
                    logger.info(
                        f"Using existing dynamic greeting audio for lead {lead_id} (legacy format)"
                    )
                    return None

            # Synthesize per lead with resolved variables
            # Apply transformation functions from payload schema if available
            resolved_payload = {}
            expected_schema = template.expected_payload_schema or {}

            for key, value in (payload or {}).items():
                resolved_value = value

                # Check if there are transformation functions for this field
                if key in expected_schema and isinstance(expected_schema[key], dict):
                    field_schema = expected_schema[key]
                    function_names = None
                    raw = field_schema.get("function")
                    if isinstance(raw, list):
                        function_names = raw
                    elif isinstance(raw, str):
                        function_names = [raw]

                    if function_names:
                        for fn_name in function_names:
                            if fn_name not in TEMPLATE_FUNCTION_REGISTRY:
                                logger.warning(
                                    f"Unknown transformation function '{fn_name}' "
                                    f"for field '{key}', skipping"
                                )
                                continue
                            try:
                                func = TEMPLATE_FUNCTION_REGISTRY[fn_name]
                                if resolved_value is None or resolved_value == "":
                                    resolved_value = func()
                                else:
                                    resolved_value = func(resolved_value)
                                logger.info(
                                    f"Applied function '{fn_name}' to field '{key}', "
                                    f"result: '{resolved_value}'"
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Error applying function '{fn_name}' to field '{key}': {e}"
                                )

                resolved_payload[key] = resolved_value

            # Resolve greeting with transformed values
            resolved_greeting = initial_greeting
            for key, value in resolved_payload.items():
                placeholder = f"{{{key}}}"
                if value is not None and isinstance(value, (str, int, float, bool)):
                    resolved_greeting = resolved_greeting.replace(
                        placeholder, str(value)
                    )

            # Get voice name: check payload first (set during push lead), then template config, then default
            voice_name = None
            payload_voice = (payload or {}).get("tts_voice_name")
            if payload_voice:
                try:
                    voice_name = TTSVoiceName(payload_voice).value
                    logger.info(
                        f"Using TTS voice '{voice_name}' from payload for greeting (lead {lead_id})"
                    )
                except ValueError:
                    logger.warning(
                        f"Invalid TTS voice '{payload_voice}' in payload, falling back to template config"
                    )

            # Fall back to template config or default
            if not voice_name:
                if template.configurations.tts_voice_name:
                    voice_name = (
                        template.configurations.tts_voice_name.value
                        if hasattr(template.configurations.tts_voice_name, "value")
                        else str(template.configurations.tts_voice_name)
                    )
                else:
                    voice_name = "rhea"

            logger.info(
                f"Synthesizing dynamic greeting for lead {lead_id}: {resolved_greeting[:50]}..."
            )
            greeting_audio = await generate_audio(
                text=resolved_greeting,
                voice_name=voice_name,
                configurations=template.configurations,
            )

            # Store audio and text as single JSON object in Redis (temporary, deleted after use)
            greeting_data = {
                "audio": base64.b64encode(greeting_audio).decode("utf-8"),
                "text": resolved_greeting,
            }
            await redis.set(
                key=lead_greeting_key,
                value=json.dumps(greeting_data),
                ex=600,  # 10-minute TTL safety net — greeting consumed within seconds
            )
            logger.info(f"Stored dynamic greeting in Redis for lead {lead_id}")
            return resolved_greeting

        else:
            # Static greeting - check if template audio already exists
            template_audio_key = f"greeting:template:{template.id}"
            existing_audio = await redis.get(template_audio_key)

            if existing_audio:
                logger.info(
                    f"Using existing static greeting audio for template {template.id}"
                )
                # For static greetings, the greeting text is the initial_greeting itself
                return initial_greeting

            # Synthesize and store as template audio (persistent, not deleted after use)
            voice_name = "rhea"
            if template.configurations.tts_voice_name:
                voice_name = (
                    template.configurations.tts_voice_name.value
                    if hasattr(template.configurations.tts_voice_name, "value")
                    else str(template.configurations.tts_voice_name)
                )

            logger.info(
                f"Synthesizing static greeting for template {template.id}: {initial_greeting[:50]}..."
            )
            greeting_audio = await generate_audio(
                text=initial_greeting,
                voice_name=voice_name,
                configurations=template.configurations,
            )
            await redis.set(
                key=template_audio_key,
                value=base64.b64encode(greeting_audio).decode("utf-8"),
            )
            logger.info(
                f"Stored static greeting audio in Redis for template {template.id}"
            )
            # For static greetings, return the initial_greeting text
            return initial_greeting

    except Exception as e:
        logger.error(
            f"Failed to synthesize/store greeting audio for lead {lead_id}: {e}"
        )
        # Continue without greeting audio - not a fatal error
        return None

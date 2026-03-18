"""Utility functions for call manager."""

import base64
import json
from typing import Optional

from app.ai.voice.agents.breeze_buddy.template.types import (
    TemplateModel,
    TTSProvider,
    VoiceConfig,
)
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
            resolved_greeting = initial_greeting
            for key, value in (payload or {}).items():
                placeholder = f"{{{key}}}"
                if value is not None and isinstance(value, (str, int, float, bool)):
                    resolved_greeting = resolved_greeting.replace(
                        placeholder, str(value)
                    )

            # Build voice config: check payload override first, then template config
            voice_config = None
            payload_provider = (payload or {}).get("tts_provider")
            if payload_provider:
                try:
                    voice_config = VoiceConfig(provider=TTSProvider(payload_provider))
                    logger.info(
                        f"Using TTS provider '{payload_provider}' from payload for greeting (lead {lead_id})"
                    )
                except ValueError:
                    logger.warning(
                        f"Invalid TTS provider '{payload_provider}' in payload, falling back to template config"
                    )

            # Fall back to template voice_config (resolve_voice_config handles None)
            if not voice_config and template.configurations.voice_config:
                voice_config = template.configurations.voice_config

            logger.info(
                f"Synthesizing dynamic greeting for lead {lead_id}: {resolved_greeting[:50]}..."
            )
            greeting_audio = await generate_audio(
                text=resolved_greeting,
                voice_config=voice_config,
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

            logger.info(
                f"Synthesizing static greeting for template {template.id}: {initial_greeting[:50]}..."
            )
            greeting_audio = await generate_audio(
                text=initial_greeting,
                voice_config=template.configurations.voice_config,
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

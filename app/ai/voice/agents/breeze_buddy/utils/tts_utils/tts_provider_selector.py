"""TTS provider selection utilities using LLM-based payload analysis."""

import asyncio
import json
from typing import Any, Dict, List, Optional

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.llm import GoogleLLMService

from app.ai.voice.agents.breeze_buddy.template.types import TTSProvider, TTSVoiceName
from app.core.config.static import GEMINI_API_KEY
from app.core.logger import logger

# Gemini model for TTS provider selection
GEMINI_TTS_SELECTION_MODEL = "gemini-2.5-flash"

# Timeout for LLM inference calls (in seconds)
LLM_INFERENCE_TIMEOUT_SECS = 10

# Mapping from TTS provider to existing voice name
TTS_PROVIDER_TO_VOICE_NAME: Dict[str, TTSVoiceName] = {
    TTSProvider.ELEVENLABS.value: TTSVoiceName.RHEA,
    TTSProvider.CARTESIA.value: TTSVoiceName.MIRA,
}


async def detect_tts_provider_from_payload(
    payload: Dict[str, Any],
    prompt: str,
    allowed_providers: List[TTSProvider],
) -> Optional[str]:
    """
    Detect the optimal TTS provider from the lead payload using LLM.

    Uses Pipecat's GoogleLLMService.run_inference() for a one-shot Gemini call.
    Sends the template-defined prompt along with the payload to Gemini,
    which returns one of the allowed provider names.

    Args:
        payload: The complete lead payload containing customer details
        prompt: Template-defined prompt with rules for provider selection
        allowed_providers: List of allowed TTS providers the LLM can choose from

    Returns:
        Provider name (e.g., "elevenlabs", "cartesia") or None if detection fails
    """
    if not payload or not GEMINI_API_KEY:
        logger.warning(
            "Cannot detect TTS provider: payload is empty or GEMINI_API_KEY not set"
        )
        return None

    if not allowed_providers:
        logger.warning("Cannot detect TTS provider: allowed_providers is empty or None")
        return None

    provider_names = [p.value for p in allowed_providers]
    payload_str = json.dumps(payload, indent=2, default=str)

    full_prompt = f"""{prompt}

Payload:
{payload_str}

You MUST return ONLY one of these provider names: {", ".join(provider_names)}
Do not return anything else. Just the provider name."""

    try:
        llm = GoogleLLMService(
            api_key=GEMINI_API_KEY,
            model=GEMINI_TTS_SELECTION_MODEL,
            params=GoogleLLMService.InputParams(
                max_tokens=200,
                temperature=0,
            ),
        )

        context = LLMContext(messages=[{"role": "user", "content": full_prompt}])

        # Wrap inference call with timeout to prevent hanging
        response = await asyncio.wait_for(
            llm.run_inference(context),
            timeout=LLM_INFERENCE_TIMEOUT_SECS,
        )

        if not response:
            logger.warning("Gemini returned empty response for TTS provider selection")
            return None

        provider_result = response.strip().lower()
        logger.debug(f"Gemini TTS provider selection response: {provider_result}")

        if provider_result in provider_names:
            logger.info(f"LLM selected TTS provider: {provider_result}")
            return provider_result
        else:
            logger.warning(
                f"LLM returned unsupported TTS provider: '{provider_result}', "
                f"allowed: {provider_names}"
            )
            return None

    except asyncio.TimeoutError:
        logger.warning(
            f"Gemini TTS provider selection timed out after {LLM_INFERENCE_TIMEOUT_SECS}s"
        )
        return None
    except Exception as e:
        logger.warning(f"Gemini TTS provider selection error: {e}")
        return None


async def determine_tts_voice_for_call(
    template_configurations,
    payload: Dict[str, Any],
    request_id: str = "unknown",
) -> Optional[TTSVoiceName]:
    """
    Determine the TTS voice name for a call based on template config and payload.

    Checks if payload-based TTS selection is enabled in the template configuration,
    and if so, uses LLM to select the optimal provider, then maps it to the
    corresponding voice name (e.g., elevenlabs → rhea, cartesia → mira).

    Args:
        template_configurations: Template configurations object
            (may have tts_selection_config attribute)
        payload: The lead/order payload containing customer details
        request_id: Request ID for logging purposes

    Returns:
        TTSVoiceName (e.g., RHEA, MIRA) or None if selection is disabled
        or detection fails (caller should fall back to existing logic)
    """
    if not template_configurations:
        logger.debug(
            f"No configurations in template for TTS provider selection, "
            f"request {request_id}"
        )
        return None

    tts_selection_config = getattr(
        template_configurations, "tts_selection_config", None
    )

    if not tts_selection_config or not tts_selection_config.enabled:
        logger.debug(f"TTS provider selection not enabled for request {request_id}")
        return None

    logger.info(f"Attempting LLM-based TTS provider selection for request {request_id}")

    provider = await detect_tts_provider_from_payload(
        payload=payload,
        prompt=tts_selection_config.prompt,
        allowed_providers=tts_selection_config.providers,
    )

    if not provider:
        logger.info(
            f"Could not determine TTS provider from payload for request {request_id}, "
            f"will fall back to template/default config"
        )
        return None

    voice_name = TTS_PROVIDER_TO_VOICE_NAME.get(provider)
    if voice_name:
        logger.info(
            f"LLM selected TTS provider '{provider}' → voice '{voice_name.value}' "
            f"for request {request_id}"
        )
    else:
        logger.warning(
            f"No voice name mapping for provider '{provider}', request {request_id}"
        )

    return voice_name

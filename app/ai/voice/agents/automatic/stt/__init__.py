from typing import Optional

from pipecat.transcriptions.language import Language

from app.ai.voice.agents.automatic.types import VoiceName
from app.ai.voice.stt import (
    DeepgramConfig,
    SarvamConfig,
    SonioxConfig,
    build_assemblyai_stt,
    build_deepgram_stt,
    build_google_stt,
    build_openai_stt,
    build_sarvam_stt,
    build_soniox_stt,
)
from app.core.config.dynamic import (
    SARVAM_STT_HIGH_VAD_SENSITIVITY,
    SARVAM_STT_LANGUAGE_CODE,
    SARVAM_STT_MODEL,
    SARVAM_STT_PROMPT,
    SARVAM_STT_VAD_SIGNALS,
)
from app.core.config.static import (
    ASSEMBLYAI_API_KEY,
    AUTOMATIC_OPENAI_STT_PROMPT,
    DEEPGRAM_API_KEY,
    ENABLE_OPENAI_FOR_MIA,
    ENFORCED_OPENAI_STT_MODEL,
    GOOGLE_CREDENTIALS_JSON,
    OPENAI_STT_API_KEY,
    OPENAI_STT_MODEL,
    SAMPLE_RATE,
    SARVAM_API_KEY,
    SONIOX_API_KEY,
    SONIOX_CONTEXT,
    SONIOX_LANGUAGE_HINTS,
    SONIOX_MAX_ENDPOINT_DELAY_MS,
    SONIOX_MODEL,
    SONIOX_VAD_FORCE_TURN_ENDPOINT,
    STT_PROVIDER,
)
from app.core.logger import logger


async def get_stt_service(voice_name: Optional[str] = None):
    """
    Returns an STT service instance based on the environment configuration.

    Args:
        voice_name: Voice name to determine STT provider override for specific voices
    """
    # Check for MIA voice with OpenAI override
    if voice_name == VoiceName.MIA.value and ENABLE_OPENAI_FOR_MIA:
        if not OPENAI_STT_API_KEY:
            raise ValueError(
                "OPENAI_STT_API_KEY is required when ENABLE_OPENAI_FOR_MIA=true and voice is MIA"
            )

        logger.info(
            f"Using OpenAI STT service for MIA voice (override enabled) with model: {ENFORCED_OPENAI_STT_MODEL}"
        )
        return build_openai_stt(
            api_key=OPENAI_STT_API_KEY,
            model=ENFORCED_OPENAI_STT_MODEL,
            language=Language.EN,
            prompt=AUTOMATIC_OPENAI_STT_PROMPT,
            temperature=0.0,
        )

    # Check for RHEA voice or Sarvam STT provider
    if STT_PROVIDER == "sarvam" or voice_name == VoiceName.RHEA.value:
        if not SARVAM_API_KEY:
            raise ValueError("SARVAM_API_KEY is required when STT_PROVIDER=sarvam")

        # Get dynamic config values from Redis (same pattern as ENABLE_BACKGROUND_TASKS in main.py)
        sarvam_stt_model = await SARVAM_STT_MODEL()
        sarvam_stt_language_code = await SARVAM_STT_LANGUAGE_CODE()
        sarvam_stt_prompt = await SARVAM_STT_PROMPT()
        sarvam_stt_vad_signals = await SARVAM_STT_VAD_SIGNALS()
        sarvam_stt_high_vad_sensitivity = await SARVAM_STT_HIGH_VAD_SENSITIVITY()

        # Pass raw config values - model-specific logic is handled internally by build_sarvam_stt
        return build_sarvam_stt(
            SarvamConfig(
                api_key=SARVAM_API_KEY,
                model=sarvam_stt_model,
                sample_rate=SAMPLE_RATE,
                language_code=sarvam_stt_language_code,
                prompt=sarvam_stt_prompt,
                vad_signals=sarvam_stt_vad_signals,
                high_vad_sensitivity=sarvam_stt_high_vad_sensitivity,
            )
        )

    # Default behavior - use configured STT provider
    if STT_PROVIDER == "assemblyai":
        if not ASSEMBLYAI_API_KEY:
            raise ValueError(
                "ASSEMBLYAI_API_KEY is required when STT_PROVIDER=assemblyai"
            )

        return build_assemblyai_stt(api_key=ASSEMBLYAI_API_KEY)
    elif STT_PROVIDER == "openai":
        if not OPENAI_STT_API_KEY:
            raise ValueError(
                "OPENAI_STT_API_KEY or OPENAI_API_KEY is required when STT_PROVIDER=openai"
            )

        logger.info(
            f"Using OpenAI STT service ({OPENAI_STT_MODEL}) with Silero VAD-based turn detection"
        )
        return build_openai_stt(
            api_key=OPENAI_STT_API_KEY,
            model=OPENAI_STT_MODEL,
            language=Language.EN,
            prompt=AUTOMATIC_OPENAI_STT_PROMPT,
            temperature=0.0,
        )
    elif STT_PROVIDER == "deepgram":
        if not DEEPGRAM_API_KEY:
            raise ValueError("DEEPGRAM_API_KEY is required when STT_PROVIDER=deepgram")

        # Defaults match DeepgramSTTConfig in template types
        return build_deepgram_stt(
            DeepgramConfig(
                api_key=DEEPGRAM_API_KEY,
                model="nova-3-general",
                language="en",
                endpointing=25,
                smart_format=True,
                punctuate=True,
                numerals=True,
                interim_results=True,
            )
        )
    elif STT_PROVIDER == "soniox":
        if not SONIOX_API_KEY:
            raise ValueError("SONIOX_API_KEY is required when STT_PROVIDER=soniox")

        # Pass raw config values - language hints parsing is handled internally by build_soniox_stt
        return build_soniox_stt(
            SonioxConfig(
                api_key=SONIOX_API_KEY,
                model=SONIOX_MODEL,
                vad_force_turn_endpoint=SONIOX_VAD_FORCE_TURN_ENDPOINT,
                language_hints=SONIOX_LANGUAGE_HINTS,
                context_json=SONIOX_CONTEXT,
                max_endpoint_delay_ms=SONIOX_MAX_ENDPOINT_DELAY_MS,
                log_context="Automatic",
            )
        )
    else:  # Default to Google STT
        logger.info("Using Google STT service with VAD-based turn detection")

        return build_google_stt(credentials_json=GOOGLE_CREDENTIALS_JSON)

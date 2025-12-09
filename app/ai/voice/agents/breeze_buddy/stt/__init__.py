from pipecat.transcriptions.language import Language

from app.ai.voice.stt import (
    SarvamConfig,
    SonioxConfig,
    build_google_stt,
    build_openai_stt,
    build_sarvam_stt,
    build_soniox_stt,
)
from app.core.config.dynamic import (
    BB_SARVAM_STT_HIGH_VAD_SENSITIVITY,
    BB_SARVAM_STT_LANGUAGE_CODE,
    BB_SARVAM_STT_MODEL,
    BB_SARVAM_STT_PROMPT,
    BB_SARVAM_STT_VAD_SIGNALS,
)
from app.core.config.static import (
    BREEZE_BUDDY_SONIOX_CONTEXT,
    BREEZE_BUDDY_SONIOX_ENABLE_NON_FINAL_TOKENS,
    BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS,
    BREEZE_BUDDY_SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS,
    BREEZE_BUDDY_SONIOX_MODEL,
    BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT,
    BREEZE_BUDDY_STT_SERVICE,
    GOOGLE_CREDENTIALS_JSON,
    OPENAI_STT_API_KEY,
    OPENAI_STT_MODEL,
    SAMPLE_RATE,
    SARVAM_API_KEY,
    SONIOX_API_KEY,
)
from app.core.logger import logger


async def get_stt_service():
    """
    Returns an STT service instance based on the environment configuration.
    """
    if BREEZE_BUDDY_STT_SERVICE == "sarvam":
        if not SARVAM_API_KEY:
            raise ValueError(
                "SARVAM_API_KEY is required when BREEZE_BUDDY_STT_SERVICE=sarvam"
            )

        # Get Breeze Buddy-specific dynamic config values from Redis
        bb_sarvam_stt_model = await BB_SARVAM_STT_MODEL()
        bb_sarvam_stt_language_code = await BB_SARVAM_STT_LANGUAGE_CODE()
        bb_sarvam_stt_prompt = await BB_SARVAM_STT_PROMPT()
        bb_sarvam_stt_vad_signals = await BB_SARVAM_STT_VAD_SIGNALS()
        bb_sarvam_stt_high_vad_sensitivity = await BB_SARVAM_STT_HIGH_VAD_SENSITIVITY()

        # Pass raw config values - model-specific logic is handled internally by build_sarvam_stt
        return build_sarvam_stt(
            SarvamConfig(
                api_key=SARVAM_API_KEY,
                model=bb_sarvam_stt_model,
                sample_rate=SAMPLE_RATE,
                language_code=bb_sarvam_stt_language_code,
                prompt=bb_sarvam_stt_prompt,
                vad_signals=bb_sarvam_stt_vad_signals,
                high_vad_sensitivity=bb_sarvam_stt_high_vad_sensitivity,
            )
        )
    elif BREEZE_BUDDY_STT_SERVICE == "openai":
        logger.info("Using OpenAI STT service for Breeze Buddy voice")
        return build_openai_stt(
            api_key=OPENAI_STT_API_KEY,
            model=OPENAI_STT_MODEL,
            language=Language.EN,
            temperature=0.0,
        )
    elif BREEZE_BUDDY_STT_SERVICE == "soniox":
        # Pass raw config values - language hints parsing is handled internally by build_soniox_stt
        return build_soniox_stt(
            SonioxConfig(
                api_key=SONIOX_API_KEY,
                model=BREEZE_BUDDY_SONIOX_MODEL,
                vad_force_turn_endpoint=BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT,
                language_hints=BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS,
                context_json=BREEZE_BUDDY_SONIOX_CONTEXT,
                enable_non_final_tokens=BREEZE_BUDDY_SONIOX_ENABLE_NON_FINAL_TOKENS,
                max_non_final_tokens_duration_ms=BREEZE_BUDDY_SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS,
                log_context="Breeze Buddy",
            )
        )
    else:
        logger.info("Using Google STT service with VAD-based turn detection")
        return build_google_stt(credentials_json=GOOGLE_CREDENTIALS_JSON)

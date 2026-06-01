"""Breeze Buddy STT service creation.

Central routing: accepts a normalized ``STTConfiguration`` from the template
and casts it to the provider-specific builder config. Defaults are baked into
the Pydantic models — no env/dynamic config needed (except API keys).
"""

from __future__ import annotations

from typing import Optional

from pipecat.transcriptions.language import Language

from app.ai.voice.agents.breeze_buddy.template.types import (
    DeepgramSTTConfig,
    SonioxSTTConfig,
    STTConfiguration,
    STTProvider,
)
from app.ai.voice.stt import (
    DeepgramConfig,
    SarvamConfig,
    SonioxConfig,
    build_deepgram_stt,
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
    BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS,
    BREEZE_BUDDY_SONIOX_MAX_ENDPOINT_DELAY_MS,
    BREEZE_BUDDY_SONIOX_MODEL,
    BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT,
    BREEZE_BUDDY_STT_SERVICE,
    DEEPGRAM_API_KEY,
    GOOGLE_CREDENTIALS_JSON,
    OPENAI_STT_API_KEY,
    OPENAI_STT_MODEL,
    SAMPLE_RATE,
    SARVAM_API_KEY,
    SONIOX_API_KEY,
)
from app.core.logger import logger


def _normalize_language(language: str | list[str] | None) -> str | None:
    """Normalize language to comma-separated string (for Soniox language_hints)."""
    if language is None:
        return None
    if isinstance(language, list):
        return ",".join(language)
    return language


def _deepgram_language(language: str | list[str] | None) -> str:
    """Normalize language for Deepgram (single code only, not CSV).

    Deepgram's ``language`` option accepts a single code (e.g. ``"en"``)
    or ``"multi"`` for auto-detection — not comma-separated lists.
    """
    if language is None:
        return "en"
    if isinstance(language, list):
        if len(language) > 1:
            logger.warning(
                "Deepgram supports only a single language code; "
                "using first value '{}' from {}",
                language[0],
                language,
            )
        return language[0] if language else "en"
    return language


async def create_stt_from_config(config: STTConfiguration):
    """Create STT service from normalized STTConfiguration.

    Central routing: reads ``config.provider`` and casts the normalized
    config to the provider-specific builder config. All tuning params
    come from the template config with sensible defaults baked in.
    """
    if config.provider == STTProvider.DEEPGRAM:
        if not DEEPGRAM_API_KEY:
            raise ValueError("DEEPGRAM_API_KEY is required for deepgram STT")

        # All defaults are in DeepgramSTTConfig — no env/dynamic lookup needed
        dg = config.deepgram or DeepgramSTTConfig()

        logger.info("Using Deepgram Nova-3 STT service for Breeze Buddy")
        return build_deepgram_stt(
            DeepgramConfig(
                api_key=DEEPGRAM_API_KEY,
                model=dg.model,
                language=_deepgram_language(config.language),
                auto_detect_language=dg.auto_detect_language,
                smart_format=dg.smart_format,
                punctuate=dg.punctuate,
                endpointing=dg.endpointing_ms,
                utterance_end_ms=dg.utterance_end_ms,  # None = disabled
                interim_results=True,
                profanity_filter=dg.profanity_filter,
                numerals=dg.numerals,
                diarize=dg.diarize,
            )
        )

    if config.provider == STTProvider.SONIOX:
        if not SONIOX_API_KEY:
            raise ValueError("SONIOX_API_KEY is required for soniox STT")

        sx = config.soniox
        effective_context = (
            sx.context if sx and sx.context else BREEZE_BUDDY_SONIOX_CONTEXT
        )
        effective_model = sx.model if sx and sx.model else BREEZE_BUDDY_SONIOX_MODEL

        if sx and sx.context:
            logger.info("Using template-specific Soniox context")

        language = _normalize_language(config.language)
        enable_lang_id = sx.enable_language_identification if sx else None
        return build_soniox_stt(
            SonioxConfig(
                api_key=SONIOX_API_KEY,
                model=effective_model,
                vad_force_turn_endpoint=BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT,
                language_hints=language or BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS,
                context_json=effective_context,
                max_endpoint_delay_ms=BREEZE_BUDDY_SONIOX_MAX_ENDPOINT_DELAY_MS,
                log_context="Breeze Buddy",
                language_hints_strict=bool(language),
                enable_language_identification=enable_lang_id,
            )
        )

    if config.provider == STTProvider.SARVAM:
        if not SARVAM_API_KEY:
            raise ValueError("SARVAM_API_KEY is required for sarvam STT")

        sv = config.sarvam
        bb_model = sv.model if sv and sv.model else await BB_SARVAM_STT_MODEL()
        bb_lang = (
            sv.language_code
            if sv and sv.language_code
            else await BB_SARVAM_STT_LANGUAGE_CODE()
        )

        return build_sarvam_stt(
            SarvamConfig(
                api_key=SARVAM_API_KEY,
                model=bb_model,
                sample_rate=SAMPLE_RATE,
                language_code=bb_lang,
                prompt=await BB_SARVAM_STT_PROMPT(),
                vad_signals=await BB_SARVAM_STT_VAD_SIGNALS(),
                high_vad_sensitivity=await BB_SARVAM_STT_HIGH_VAD_SENSITIVITY(),
            )
        )

    if config.provider == STTProvider.OPENAI:
        if not OPENAI_STT_API_KEY:
            raise ValueError("OPENAI_STT_API_KEY is required for openai STT")
        logger.info("Using OpenAI STT service for Breeze Buddy")
        return build_openai_stt(
            api_key=OPENAI_STT_API_KEY,
            model=OPENAI_STT_MODEL,
            language=Language.EN,
            temperature=0.0,
        )

    # Default: Google
    logger.info("Using Google STT service for Breeze Buddy")
    return build_google_stt(credentials_json=GOOGLE_CREDENTIALS_JSON)


async def get_stt_service(
    language_hints: str | None = None,
    soniox_context: str | None = None,
    stt_configuration: Optional[STTConfiguration] = None,
):
    """Returns an STT service instance.

    If ``stt_configuration`` is provided (from template), routes through
    :func:`create_stt_from_config`. Otherwise falls back to env-var-based
    provider selection (legacy path).
    """
    # --- New path: template-level STTConfiguration ---
    if stt_configuration is not None:
        return await create_stt_from_config(stt_configuration)

    # --- Legacy path: env var BREEZE_BUDDY_STT_SERVICE ---
    provider_map = {
        "soniox": STTProvider.SONIOX,
        "deepgram": STTProvider.DEEPGRAM,
        "sarvam": STTProvider.SARVAM,
        "openai": STTProvider.OPENAI,
        "google": STTProvider.GOOGLE,
    }
    provider = provider_map.get(BREEZE_BUDDY_STT_SERVICE, STTProvider.GOOGLE)

    legacy_config = STTConfiguration(
        provider=provider,
        language=language_hints,
        soniox=SonioxSTTConfig(context=soniox_context) if soniox_context else None,
    )
    return await create_stt_from_config(legacy_config)

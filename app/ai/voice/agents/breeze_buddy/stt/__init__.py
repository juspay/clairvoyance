"""Breeze Buddy STT service creation.

Central routing: accepts a normalized ``STTConfiguration`` from the template
and casts it to the provider-specific builder config. Wraps the result in
``STTServiceResult`` so callers know which provider was actually used
(may differ from requested if fallback kicked in).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

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
    BB_STT_SERVICE,
)
from app.core.config.static import (
    BREEZE_BUDDY_SONIOX_CONTEXT,
    BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS,
    BREEZE_BUDDY_SONIOX_MAX_ENDPOINT_DELAY_MS,
    BREEZE_BUDDY_SONIOX_MODEL,
    BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT,
    DEEPGRAM_API_KEY,
    GOOGLE_CREDENTIALS_JSON,
    OPENAI_STT_API_KEY,
    OPENAI_STT_MODEL,
    SAMPLE_RATE,
    SARVAM_API_KEY,
    SONIOX_API_KEY,
)
from app.core.logger import logger
from app.services.fallback import (
    BB_FALLBACK_CONFIG,
    ServiceFallback,
    ServiceFallbackConfig,
)


@dataclass
class STTServiceResult:
    """Result of STT service creation.

    ``provider`` reflects the *actual* provider used — may differ from
    the requested one if fallback kicked in (e.g. requested soniox but
    fallback forced deepgram).
    """

    provider: str
    service: Any


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


async def _build_stt_provider(config: STTConfiguration) -> Any:
    """Pure builder: create an STT service from config. No fallback logic.

    Raises on missing API keys or provider init errors — callers handle fallback.
    """
    if config.provider == STTProvider.DEEPGRAM:
        if not DEEPGRAM_API_KEY:
            raise ValueError("DEEPGRAM_API_KEY is required for deepgram STT")

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
                utterance_end_ms=dg.utterance_end_ms,
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


async def create_stt_from_config(config: STTConfiguration) -> STTServiceResult:
    """Create STT service with fallback support.

    When ``BB_FALLBACK`` DevCycle flag has ``stt.enabled: true``:
    1. If fallback is currently active, proactively routes to the fallback provider.
    2. Otherwise, wraps the build in try/except — on init failure, records the
       failure and falls back to the configured fallback provider.
    When disabled, builds directly without fallback.
    """
    cfg = await BB_FALLBACK_CONFIG("stt")
    provider_name = config.provider.value

    if not cfg.enabled:
        service = await _build_stt_provider(config)
        return STTServiceResult(provider=provider_name, service=service)

    # Fallback enabled
    fallback_provider = cfg.fallback_provider
    primary_provider = await BB_STT_SERVICE()

    def _make_fallback_obj() -> ServiceFallback:
        return ServiceFallback(
            ServiceFallbackConfig(
                service_name="stt",
                failure_threshold=cfg.threshold,
                failure_window_secs=cfg.window_secs,
                fallback_duration_secs=cfg.duration_secs,
                primary_provider_name=primary_provider,
                fallback_provider_name=fallback_provider,
            )
        )

    # Proactive routing: if fallback is active, skip primary entirely
    if provider_name != fallback_provider:
        fb = _make_fallback_obj()
        if await fb.is_active():
            logger.info(
                f"STT fallback active — using {fallback_provider} "
                f"instead of {provider_name}"
            )
            fallback_config = STTConfiguration(
                provider=STTProvider(fallback_provider),
                language=config.language,
            )
            service = await _build_stt_provider(fallback_config)
            # One-time alert that calls are routing through fallback (NX dedup in _activate)
            return STTServiceResult(provider=fallback_provider, service=service)

    # Try primary, with init-time fallback on failure
    try:
        service = await _build_stt_provider(config)
    except Exception as primary_err:
        # Skip fallback if primary == fallback (nothing to fall to)
        if provider_name == fallback_provider:
            raise

        logger.error(
            f"{provider_name.capitalize()} STT initialization failed, "
            f"falling back to {fallback_provider.capitalize()}: {primary_err}"
        )

        # Record failure (increments counter, may activate fallback circuit breaker)
        fb = _make_fallback_obj()
        await fb.record_failure(error_msg=str(primary_err)[:200], context="init")

        # Try fallback provider
        fallback_config = STTConfiguration(
            provider=STTProvider(fallback_provider),
            language=config.language,
        )
        try:
            fallback_service = await _build_stt_provider(fallback_config)
            logger.info(
                f"Successfully initialized {fallback_provider.capitalize()} STT "
                f"as fallback for {provider_name.capitalize()} failure"
            )
            return STTServiceResult(
                provider=fallback_provider, service=fallback_service
            )
        except Exception as fallback_err:
            logger.error(
                f"{fallback_provider.capitalize()} fallback also failed: {fallback_err}. "
                f"Original {provider_name.capitalize()} error: {primary_err}"
            )
            raise primary_err from fallback_err

    return STTServiceResult(provider=provider_name, service=service)


async def get_stt_service(
    language_hints: str | None = None,
    soniox_context: str | None = None,
    stt_configuration: Optional[STTConfiguration] = None,
) -> STTServiceResult:
    """Returns an STTServiceResult wrapping the STT service and actual provider.

    If ``stt_configuration`` is provided (from template), routes through
    :func:`create_stt_from_config`. Otherwise falls back to dynamic-config-based
    provider selection (legacy path — respects fallback overrides via BB_STT_SERVICE).
    """
    # --- New path: template-level STTConfiguration ---
    if stt_configuration is not None:
        return await create_stt_from_config(stt_configuration)

    # --- Legacy path: dynamic BB_STT_SERVICE (respects config:override) ---
    effective_service = await BB_STT_SERVICE()
    provider_map = {
        "soniox": STTProvider.SONIOX,
        "deepgram": STTProvider.DEEPGRAM,
        "sarvam": STTProvider.SARVAM,
        "openai": STTProvider.OPENAI,
        "google": STTProvider.GOOGLE,
    }
    provider = provider_map.get(effective_service, STTProvider.GOOGLE)

    legacy_config = STTConfiguration(
        provider=provider,
        language=language_hints,
        soniox=SonioxSTTConfig(context=soniox_context) if soniox_context else None,
    )

    return await create_stt_from_config(legacy_config)

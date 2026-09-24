"""Breeze Buddy STT service creation.

Central routing: accepts a normalized ``STTConfiguration`` from the template
and casts it to the provider-specific builder config. Defaults are baked into
the Pydantic models — no env/dynamic config needed (except API keys).
"""

from __future__ import annotations

from typing import Optional

from pipecat.services.elevenlabs.stt import CommitStrategy
from pipecat.transcriptions.language import Language

from app.ai.voice.agents.breeze_buddy.template.types import (
    AssemblyAISTTConfig,
    DeepgramSTTConfig,
    ElevenLabsSTTConfig,
    SonioxSTTConfig,
    STTConfiguration,
    STTProvider,
    TurnDetectionMode,
)
from app.ai.voice.stt import (
    AssemblyAIConfig,
    DeepgramConfig,
    ElevenLabsConfig,
    SarvamConfig,
    SonioxConfig,
    build_assemblyai_stt,
    build_deepgram_stt,
    build_elevenlabs_stt,
    build_google_stt,
    build_openai_stt,
    build_sarvam_stt,
    build_soniox_stt,
)
from app.ai.voice.stt.elevenlabs import (
    resolve_languages as resolve_elevenlabs_languages,
)
from app.core.config.dynamic import (
    BB_SARVAM_STT_HIGH_VAD_SENSITIVITY,
    BB_SARVAM_STT_LANGUAGE_CODE,
    BB_SARVAM_STT_MODEL,
    BB_SARVAM_STT_PROMPT,
    BB_SARVAM_STT_VAD_SIGNALS,
)
from app.core.config.static import (
    ASSEMBLYAI_API_KEY,
    BREEZE_BUDDY_SONIOX_CONTEXT,
    BREEZE_BUDDY_SONIOX_FINALIZE_AFTER_SECS,
    BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS,
    BREEZE_BUDDY_SONIOX_MAX_ENDPOINT_DELAY_MS,
    BREEZE_BUDDY_SONIOX_MODEL,
    BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT,
    BREEZE_BUDDY_SONIOX_WS_CLOSE_TIMEOUT,
    BREEZE_BUDDY_SONIOX_WS_PING_INTERVAL,
    BREEZE_BUDDY_SONIOX_WS_PING_TIMEOUT,
    BREEZE_BUDDY_STT_SERVICE,
    DEEPGRAM_API_KEY,
    ELEVENLABS_STT_API_KEY,
    ELEVENLABS_STT_URL,
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
        # Template field wins; 0 (= disabled) must flow through, so the env
        # default only applies when the template left it unset.
        effective_finalize_after = (
            sx.finalize_after_secs
            if sx and sx.finalize_after_secs is not None
            else BREEZE_BUDDY_SONIOX_FINALIZE_AFTER_SECS
        )
        # Same precedence for the VAD-forced endpoint trigger.
        effective_vad_force = (
            sx.vad_force_turn_endpoint
            if sx and sx.vad_force_turn_endpoint is not None
            else BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT
        )
        return build_soniox_stt(
            SonioxConfig(
                api_key=SONIOX_API_KEY,
                model=effective_model,
                vad_force_turn_endpoint=effective_vad_force,
                language_hints=language or BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS,
                context_json=effective_context,
                max_endpoint_delay_ms=BREEZE_BUDDY_SONIOX_MAX_ENDPOINT_DELAY_MS,
                log_context="Breeze Buddy",
                language_hints_strict=bool(language),
                enable_language_identification=enable_lang_id,
                finalize_after_secs=effective_finalize_after,
                ws_ping_interval=BREEZE_BUDDY_SONIOX_WS_PING_INTERVAL,
                ws_ping_timeout=BREEZE_BUDDY_SONIOX_WS_PING_TIMEOUT,
                ws_close_timeout=BREEZE_BUDDY_SONIOX_WS_CLOSE_TIMEOUT,
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

    if config.provider == STTProvider.ASSEMBLYAI:
        if not ASSEMBLYAI_API_KEY:
            raise ValueError("ASSEMBLYAI_API_KEY is required for assemblyai STT")

        aai = config.assemblyai or AssemblyAISTTConfig()

        # Who ends a turn. SMART_TURN is the exception, not STT_NATIVE: it is
        # the only mode the pipeline auto-creates a Silero VAD for, and
        # vad_force_turn_endpoint=True is reachable ONLY through that VAD.
        # TIMEOUT gets no VAD and BREEZE_BUDDY_ENABLE_VAD defaults False, so
        # forcing it here would leave the caller talking to a bot that never
        # receives a final transcript.
        vad_force_turn_endpoint = config.turn_detection == TurnDetectionMode.SMART_TURN

        # pipecat raises ValueError when AssemblyAI-side endpointing is asked
        # of a non-u3-pro model. Catch it here, where the message can name the
        # template field, instead of at connect where it kills the call.
        # Accept both names for the same family: AssemblyAI documents
        # "universal-3-5-pro", pipecat 1.1.0 hardcodes "u3-rt-pro" (the
        # builder translates between them). pipecat 1.8.1's is_u3_pro_model
        # matches both plus their -* variants.
        if not vad_force_turn_endpoint and not aai.model.startswith(
            ("universal-3-5-pro", "u3-rt-pro")
        ):
            raise ValueError(
                f"assemblyai: turn_detection={config.turn_detection.value} needs "
                f"AssemblyAI-side endpointing, which requires Universal-3.5 Pro; "
                f"got model={aai.model!r}. Set model='universal-3-5-pro' or use "
                f"turn_detection='smart_turn'."
            )

        return build_assemblyai_stt(
            AssemblyAIConfig(
                api_key=ASSEMBLYAI_API_KEY,
                model=aai.model,
                language_codes=aai.language_codes,
                vad_force_turn_endpoint=vad_force_turn_endpoint,
                keyterms_prompt=aai.keyterms_prompt,
                prompt=aai.prompt,
                end_of_turn_confidence_threshold=aai.end_of_turn_confidence_threshold,
                min_turn_silence=aai.min_turn_silence,
                max_turn_silence=aai.max_turn_silence,
                formatted_finals=aai.formatted_finals,
                format_turns=aai.format_turns,
                language_detection=aai.language_detection,
                speaker_labels=aai.speaker_labels,
                vad_threshold=aai.vad_threshold,
                word_finalization_max_wait_time=aai.word_finalization_max_wait_time,
                domain=aai.domain,
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

    if config.provider == STTProvider.ELEVENLABS:
        # Key and host are one pair: a key is only accepted by the account it
        # belongs to, so they always travel together and both come from the
        # env. Switching accounts is an env change, not a code path — which is
        # why there is no flag here. Raise now rather than let an empty key
        # reach the WebSocket: a build failure is a dead pod on deploy, an
        # auth failure is a live call that cannot hear.
        if not ELEVENLABS_STT_API_KEY:
            raise ValueError("ELEVENLABS_STT_API_KEY is required for elevenlabs STT")

        el = config.elevenlabs or ElevenLabsSTTConfig()

        # Map the normalized turn mode to ElevenLabs' commit strategy.
        # SMART_TURN is the exception, not STT_NATIVE: it is the only mode
        # that gets a VAD analyzer (pipeline.py auto-creates a Silero when
        # none is attached), and MANUAL commit is reachable ONLY through a
        # VADUserStoppedSpeakingFrame. TIMEOUT gets no VAD and
        # BREEZE_BUDDY_ENABLE_VAD defaults to False, so under MANUAL it would
        # never commit: Scribe streams interims forever, no TranscriptionFrame
        # is ever produced and the LLM is never invoked — the caller talks to
        # a bot that cannot hear, for the whole call.
        #
        # VAD commit is also correct for TIMEOUT rather than merely safe.
        # SpeechTimeoutUserTurnStopStrategy's documented fallback — "when a
        # transcript arrives without a VAD stop event, user_speech_timeout
        # measures inactivity since the last transcript, rearmed on each
        # transcript" — IS timeout semantics, and it only runs on finals.
        commit_strategy = (
            CommitStrategy.MANUAL
            if config.turn_detection == TurnDetectionMode.SMART_TURN
            else CommitStrategy.VAD
        )

        primary_language, secondary_languages = resolve_elevenlabs_languages(
            el.language_code, el.secondary_languages, config.language
        )

        logger.info(
            "Using ElevenLabs Scribe v2 Realtime STT service for Breeze Buddy "
            "(commit_strategy={})",
            commit_strategy.value,
        )
        return build_elevenlabs_stt(
            ElevenLabsConfig(
                api_key=ELEVENLABS_STT_API_KEY,
                base_url=ELEVENLABS_STT_URL,
                commit_strategy=commit_strategy,
                model=el.model,
                language_code=primary_language,
                secondary_languages=secondary_languages,
                include_language_detection=el.include_language_detection,
                include_timestamps=el.include_timestamps,
                enable_logging=el.enable_logging,
                vad_silence_threshold_secs=el.vad_silence_threshold_secs,
                vad_threshold=el.vad_threshold,
                min_speech_duration_ms=el.min_speech_duration_ms,
                min_silence_duration_ms=el.min_silence_duration_ms,
            )
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
        "elevenlabs": STTProvider.ELEVENLABS,
        "assemblyai": STTProvider.ASSEMBLYAI,
    }
    provider = provider_map.get(BREEZE_BUDDY_STT_SERVICE, STTProvider.GOOGLE)

    legacy_config = STTConfiguration(
        provider=provider,
        language=language_hints,
        soniox=SonioxSTTConfig(context=soniox_context) if soniox_context else None,
    )
    return await create_stt_from_config(legacy_config)

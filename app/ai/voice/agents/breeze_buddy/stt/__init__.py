"""Breeze Buddy STT service creation.

Central routing: accepts a normalized ``STTConfiguration`` from the template
and casts it to the provider-specific builder config. Defaults are baked into
the Pydantic models — no env/dynamic config needed (except API keys).
"""

from __future__ import annotations

from typing import Optional

from pipecat.services.elevenlabs.stt import CommitStrategy
from pipecat.transcriptions.language import Language

from app.ai.voice.agents.breeze_buddy.accounts import (
    Accounts,
    GcpAccount,
    KeyAccount,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    AssemblyAISTTConfig,
    DeepgramSTTConfig,
    ElevenLabsSTTConfig,
    SmallestSTTConfig,
    SonioxSTTConfig,
    STTConfiguration,
    STTProvider,
    TurnDetectionMode,
    set_by_template,
)
from app.ai.voice.stt import (
    AssemblyAIConfig,
    DeepgramConfig,
    DeepgramFluxConfig,
    ElevenLabsConfig,
    SarvamConfig,
    SmallestConfig,
    SonioxConfig,
    build_assemblyai_stt,
    build_deepgram_flux_stt,
    build_deepgram_stt,
    build_elevenlabs_stt,
    build_google_stt,
    build_openai_stt,
    build_sarvam_stt,
    build_smallest_stt,
    build_soniox_stt,
)
from app.ai.voice.stt.assemblyai import u3_pro_wire_name
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
    OPENAI_STT_MODEL,
    SAMPLE_RATE,
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


def _first_language(language: str | list[str] | None) -> str | None:
    """First code of the template language (single-language providers)."""
    codes = _language_list(language)
    return codes[0] if codes else None


def _language_list(language: str | list[str] | None) -> list[str]:
    """Template language as a list of codes (Flux hints, Smallest).

    The legacy path hands over a list joined into one string ("en,hi"), so
    comma-joined values are split rather than read as a single code.
    """
    if language is None:
        return []
    items = language if isinstance(language, list) else [language]
    return [code.strip() for item in items for code in item.split(",") if code.strip()]


async def create_stt_from_config(
    config: STTConfiguration,
    accounts: Optional[Accounts] = None,
    stt_self_interrupt: bool = True,
):
    """Create STT service from normalized STTConfiguration.

    Central routing: reads ``config.provider`` and casts the normalized
    config to the provider-specific builder config. All tuning params
    come from the template config with sensible defaults baked in.

    ``accounts`` is the call's account resolver (accounts):
    the block's own row when it names one, else the environment's key.
    """
    resolver = accounts or Accounts()
    if config.provider == STTProvider.DEEPGRAM:
        account = await resolver.get(config, KeyAccount)
        api_key = account.api_key

        # All defaults are in DeepgramSTTConfig — no env/dynamic lookup needed
        dg = config.deepgram or DeepgramSTTConfig()

        if dg.is_flux:
            logger.info("Using Deepgram Flux STT service for Breeze Buddy")
            return build_deepgram_flux_stt(
                DeepgramFluxConfig(
                    api_key=api_key,
                    model=dg.model,
                    language_hints=_language_list(config.language),
                    eot_threshold=dg.eot_threshold,
                    eager_eot_threshold=dg.eager_eot_threshold,
                    eot_timeout_ms=(
                        dg.eot_timeout_ms
                        if dg.eot_timeout_ms is not None
                        else config.end_of_speech_ms
                    ),
                    mip_opt_out=dg.mip_opt_out,
                )
            )

        # endpointing_ms has a non-None default; a value other than it is the
        # template's own choice and beats end_of_speech_ms (set_by_template:
        # model_fields_set does not survive a template save or cache).
        endpointing = (
            dg.endpointing_ms
            if set_by_template(dg, "endpointing_ms") or config.end_of_speech_ms is None
            else config.end_of_speech_ms
        )
        logger.info("Using Deepgram Nova-3 STT service for Breeze Buddy")
        return build_deepgram_stt(
            DeepgramConfig(
                api_key=api_key,
                model=dg.model,
                language=_deepgram_language(config.language),
                auto_detect_language=dg.auto_detect_language,
                smart_format=dg.smart_format,
                punctuate=dg.punctuate,
                endpointing=endpointing,
                utterance_end_ms=dg.utterance_end_ms,  # None = disabled
                interim_results=True,
                profanity_filter=dg.profanity_filter,
                numerals=dg.numerals,
                diarize=dg.diarize,
            )
        )

    if config.provider == STTProvider.SONIOX:
        account = await resolver.get(config, KeyAccount)
        api_key = account.api_key

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
        # Endpoint cap: end_of_speech_ms (validated 500-3000), else env.
        effective_max_endpoint_delay = (
            config.end_of_speech_ms or BREEZE_BUDDY_SONIOX_MAX_ENDPOINT_DELAY_MS
        )
        # Same precedence for the VAD-forced endpoint trigger.
        effective_vad_force = (
            sx.vad_force_turn_endpoint
            if sx and sx.vad_force_turn_endpoint is not None
            else BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT
        )
        if effective_vad_force and config.end_of_speech_ms is not None:
            # Refused at parse when the template forces it; here the env did.
            logger.warning(
                "soniox: end_of_speech_ms={} has no effect: VAD-forced endpoints "
                "(BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT) disable Soniox's "
                "own endpoint detection",
                config.end_of_speech_ms,
            )
        return build_soniox_stt(
            SonioxConfig(
                api_key=api_key,
                model=effective_model,
                vad_force_turn_endpoint=effective_vad_force,
                language_hints=language or BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS,
                context_json=effective_context,
                max_endpoint_delay_ms=effective_max_endpoint_delay,
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
        account = await resolver.get(config, KeyAccount)
        api_key = account.api_key

        sv = config.sarvam
        bb_model = sv.model if sv and sv.model else await BB_SARVAM_STT_MODEL()
        # saaras models never received a language before this change (they
        # auto-detected), so they pin only one the TEMPLATE names. The global
        # Redis default keeps reaching saarika only, as it always has: a key
        # set in an environment must not silently lock every saaras template.
        if sv and sv.language_code:
            bb_lang = sv.language_code
        elif "saaras" in bb_model.lower():
            bb_lang = None
        else:
            bb_lang = await BB_SARVAM_STT_LANGUAGE_CODE()

        return build_sarvam_stt(
            SarvamConfig(
                # Sarvam's own barge-in fires on the first sound; off when the
                # template's interruption rule must decide (see pipeline.py).
                self_interrupt=stt_self_interrupt,
                api_key=api_key,
                model=bb_model,
                sample_rate=SAMPLE_RATE,
                language_code=bb_lang,
                prompt=await BB_SARVAM_STT_PROMPT(),
                vad_signals=await BB_SARVAM_STT_VAD_SIGNALS(),
                high_vad_sensitivity=await BB_SARVAM_STT_HIGH_VAD_SENSITIVITY(),
            )
        )

    if config.provider == STTProvider.ASSEMBLYAI:
        account = await resolver.get(config, KeyAccount)
        assemblyai_key = account.api_key

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
        if not vad_force_turn_endpoint and not u3_pro_wire_name(aai.model):
            raise ValueError(
                f"assemblyai: turn_detection={config.turn_detection.value} needs "
                f"AssemblyAI-side endpointing, which requires a Universal-3.x Pro "
                f"model; got model={aai.model!r}. Set model='universal-3-5-pro' "
                f"or 'universal-3-6-pro', or use "
                f"turn_detection='smart_turn'."
            )

        # max_turn_silence defaults to 1000: a value other than it is the
        # template's own choice; otherwise end_of_speech_ms, else 1000.
        # A shorter max than min_turn_silence would be refused by AssemblyAI,
        # so min follows it down.
        # An explicit null reads as unset here, so end_of_speech_ms applies;
        # with neither, null still reaches AssemblyAI as before (its default).
        own_value = aai.max_turn_silence is not None and set_by_template(
            aai, "max_turn_silence"
        )
        max_turn_silence = (
            aai.max_turn_silence
            if own_value or config.end_of_speech_ms is None
            else config.end_of_speech_ms
        )
        min_turn_silence = aai.min_turn_silence
        if min_turn_silence is not None and max_turn_silence is not None:
            min_turn_silence = min(min_turn_silence, max_turn_silence)

        return build_assemblyai_stt(
            AssemblyAIConfig(
                api_key=assemblyai_key,
                model=aai.model,
                language_codes=aai.language_codes,
                vad_force_turn_endpoint=vad_force_turn_endpoint,
                keyterms_prompt=aai.keyterms_prompt,
                prompt=aai.prompt,
                end_of_turn_confidence_threshold=aai.end_of_turn_confidence_threshold,
                min_turn_silence=min_turn_silence,
                max_turn_silence=max_turn_silence,
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
        account = await resolver.get(config, KeyAccount)
        api_key = account.api_key
        logger.info("Using OpenAI STT service for Breeze Buddy")
        return build_openai_stt(
            api_key=api_key,
            model=OPENAI_STT_MODEL,
            language=Language.EN,
            temperature=0.0,
        )

    if config.provider == STTProvider.ELEVENLABS:
        # Key and host are one pair: the account carries both (the row's key
        # or the residency env key, always on the India-resident host —
        # accounts.elevenlabs_host). pipecat builds ``wss://{base_url}/v1``
        # itself, so it gets the bare host.
        account = await resolver.get(config, KeyAccount)
        api_key = account.api_key
        base_url = str(account.endpoint).removeprefix("wss://")

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
                api_key=api_key,
                base_url=base_url,
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

    if config.provider == STTProvider.SMALLEST:
        account = await resolver.get(config, KeyAccount)
        sm = config.smallest or SmallestSTTConfig()
        return build_smallest_stt(
            SmallestConfig(
                api_key=account.api_key,
                # the block's language, else the template's, else Hindi
                language=sm.language or _first_language(config.language) or "hi",
                numerals=sm.numerals,
            )
        )

    # Default: Google
    logger.info("Using Google STT service for Breeze Buddy")
    account = await resolver.get(config, GcpAccount)
    return build_google_stt(credentials_json=account.credentials_json)


async def get_stt_service(
    language_hints: str | None = None,
    soniox_context: str | None = None,
    stt_configuration: Optional[STTConfiguration] = None,
    accounts: Optional[Accounts] = None,
    stt_self_interrupt: bool = True,
):
    """Returns an STT service instance.

    If ``stt_configuration`` is provided (from template), routes through
    :func:`create_stt_from_config`. Otherwise falls back to env-var-based
    provider selection (legacy path). ``accounts`` rides through to the
    builder (the call's account resolver, see accounts).
    """
    # --- New path: template-level STTConfiguration ---
    if stt_configuration is not None:
        return await create_stt_from_config(
            stt_configuration,
            accounts=accounts,
            stt_self_interrupt=stt_self_interrupt,
        )

    # --- Legacy path: env var BREEZE_BUDDY_STT_SERVICE ---
    provider_map = {
        "soniox": STTProvider.SONIOX,
        "deepgram": STTProvider.DEEPGRAM,
        "sarvam": STTProvider.SARVAM,
        "openai": STTProvider.OPENAI,
        "google": STTProvider.GOOGLE,
        "elevenlabs": STTProvider.ELEVENLABS,
        "assemblyai": STTProvider.ASSEMBLYAI,
        "smallest": STTProvider.SMALLEST,
    }
    provider = provider_map.get(BREEZE_BUDDY_STT_SERVICE, STTProvider.GOOGLE)

    legacy_config = STTConfiguration(
        provider=provider,
        language=language_hints,
        soniox=SonioxSTTConfig(context=soniox_context) if soniox_context else None,
    )
    return await create_stt_from_config(
        legacy_config, stt_self_interrupt=stt_self_interrupt
    )

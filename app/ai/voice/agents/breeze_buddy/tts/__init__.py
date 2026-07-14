"""TTS service utilities for Breeze Buddy voice agent."""

from pipecat.services.cartesia.tts import GenerationConfig
from pipecat.transcriptions.language import Language

from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    TTSConfig,
    TTSProvider,
)
from app.ai.voice.agents.breeze_buddy.tts.dragontts.health import (
    is_dragontts_healthy,
)
from app.ai.voice.agents.breeze_buddy.tts.emoji_filter import (
    EmojiTextFilter,
    strip_emojis,
)
from app.ai.voice.agents.breeze_buddy.utils.common import convert_to_mulaw
from app.ai.voice.tts import (
    CartesiaConfig,
    DragonTTSConfig,
    ElevenLabsConfig,
    GeminiConfig,
    GoogleConfig,
    SarvamTTSConfig,
    SonioxTTSConfig,
    build_cartesia_tts,
    build_dragontts_tts,
    build_elevenlabs_tts,
    build_gemini_tts,
    build_google_tts,
    build_sarvam_tts,
    build_soniox_tts,
)
from app.ai.voice.tts.cartesia import _generate_cartesia_audio
from app.ai.voice.tts.dragontts import _collect_params, _generate_dragontts_audio
from app.ai.voice.tts.elevenlabs import _generate_elevenlabs_audio
from app.ai.voice.tts.gemini import _generate_gemini_audio
from app.ai.voice.tts.google import _generate_google_audio
from app.ai.voice.tts.sarvam import _generate_sarvam_audio
from app.ai.voice.tts.soniox import _generate_soniox_audio
from app.core.config.dynamic import (
    BB_AGGREGATE_SENTENCES,
    BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY,
    BB_SARVAM_TTS_ENABLE_PREPROCESSING,
    BB_STRIP_EMOJIS_FROM_TTS,
    BB_TTS_SERVICE,
    BB_VOICE_PROVIDER_DEFAULTS,
    DRAGONTTS_URL,
)
from app.core.config.static import (
    CARTESIA_API_KEY,
    ELEVENLABS_API_KEY,
    ELEVENLABS_INDIAN_RESIDENCY_API_KEY,
    ELEVENLABS_INDIAN_RESIDENCY_WEBSOCKET_URL,
    GOOGLE_CREDENTIALS_JSON,
    SARVAM_API_KEY,
    SONIOX_API_KEY,
)
from app.core.logger import logger

_VOICE_CONFIG_FIELDS = (
    "voice_id",
    "model",
    "language",
    "speed",
    "volume",
    "emotion",
    "pitch",
    "style_prompt",
    "enable_ssml_parsing",
    "enable_tts_caching",
)


async def resolve_voice_config(
    template_voice_config: TTSConfig | None = None,
    overrides: dict[str, TTSConfig] | None = None,
) -> TTSConfig:
    """Merge template-level TTSConfig with Redis/hardcoded defaults.

    Resolution order for a given provider:
      1. voice_config_overrides[provider]  (per-provider template settings)
      2. voice_config                      (default template settings, if same provider)
      3. BB_VOICE_PROVIDER_DEFAULTS        (Redis / hardcoded)

    Args:
        template_voice_config: The template's default voice_config.
        overrides: Per-provider voice configs (from voice_config_overrides).
    """
    if template_voice_config:
        provider = template_voice_config.provider.value
    else:
        provider = await BB_TTS_SERVICE()

    # Validate provider early — bad Redis value shouldn't crash call startup
    try:
        provider_enum = TTSProvider(provider)
    except ValueError:
        logger.warning(f"Unknown TTS provider '{provider}', falling back to elevenlabs")
        provider = "elevenlabs"
        provider_enum = TTSProvider.ELEVENLABS

    # Pick the most specific config for this provider
    effective_config = (overrides or {}).get(provider) or template_voice_config

    defaults = await BB_VOICE_PROVIDER_DEFAULTS(provider)

    if not effective_config:
        return TTSConfig(provider=provider_enum, **defaults)

    # Merge: effective_config fields win over defaults for non-None values
    merged = {}
    for field in _VOICE_CONFIG_FIELDS:
        val = getattr(effective_config, field, None)
        merged[field] = val if val is not None else defaults.get(field)

    return TTSConfig(provider=effective_config.provider, **merged)


def _parse_language(code: str | None, fallback: Language = Language.EN) -> Language:
    """Convert a language code string to a pipecat Language enum."""
    if not code:
        return fallback
    try:
        return Language[code.upper().replace("-", "_")]
    except KeyError:
        logger.warning(
            f"Language code '{code}' not found in Language enum, using {fallback}"
        )
        return fallback


async def get_tts_service(voice_config: TTSConfig):
    """Build a TTS service from a resolved TTSConfig."""
    provider = voice_config.provider.value

    # Route through the DragonTTS caching proxy when the template selects it
    # directly (legacy provider="dragontts") OR opts in via enable_tts_caching
    # AND DragonTTS is currently healthy. When DragonTTS is down the health flag
    # is "0", so enable_tts_caching templates fall through to their upstream
    # provider directly (graceful — calls work, just uncached). Legacy
    # provider="dragontts" is intentionally not health-gated.
    if provider == "dragontts" or (
        voice_config.enable_tts_caching is True and await is_dragontts_healthy()
    ):
        if provider == "dragontts":
            # Legacy: model already carries "<provider>:<model>".
            model_id = voice_config.model
            if not model_id:
                raise ValueError("dragontts requires model '<provider>:<model>'")
            nested = model_id.split(":", 1)
            if len(nested) != 2 or not nested[0] or not nested[1]:
                raise ValueError(
                    f"dragontts model must be '<provider>:<model>' with non-empty "
                    f"parts, got {model_id!r}"
                )
            nested_provider = nested[0]
            nested_model = nested[1]
        else:
            # Auto-wrap: provider is the upstream; build the proxy model id.
            if not voice_config.model:
                raise ValueError("enable_tts_caching requires a model")
            nested_provider = provider
            nested_model = voice_config.model
            model_id = f"{provider}:{voice_config.model}"

        aggregate = await BB_AGGREGATE_SENTENCES(nested_provider)

        logger.info(
            f"Building DragonTTS streaming service: nested_provider={nested_provider}, "
            f"model={nested_model}, voice_id={voice_config.voice_id}, "
            f"language={voice_config.language}"
        )

        return build_dragontts_tts(
            DragonTTSConfig(
                url=await DRAGONTTS_URL(),
                model_id=model_id,  # full "<provider>:<model>"
                voice_id=voice_config.voice_id or "",
                language=voice_config.language or "",
                params=_collect_params(voice_config),
                aggregate_sentences=aggregate,
            )
        )

    logger.info(
        f"Building TTS service: provider={provider}, voice_id={voice_config.voice_id}, "
        f"model={voice_config.model}, speed={voice_config.speed}, language={voice_config.language}, "
        f"enable_ssml_parsing={voice_config.enable_ssml_parsing}"
    )

    # Emoji stripping applies to EVERY provider/flow. pipecat runs these filters
    # only on the string sent to the TTS provider (incl. TTSSpeakFrame used by
    # the widget stream mode + fillers); the transcript frame keeps its emoji.
    text_filters: list = []
    if await BB_STRIP_EMOJIS_FROM_TTS():
        text_filters.append(EmojiTextFilter())

    if provider == "elevenlabs":
        use_indian_residency = await BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY()
        if use_indian_residency and not ELEVENLABS_INDIAN_RESIDENCY_API_KEY:
            raise ValueError(
                "ELEVENLABS_INDIAN_RESIDENCY_API_KEY is required when BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY is True"
            )
        if not use_indian_residency and not ELEVENLABS_API_KEY:
            raise ValueError("ELEVENLABS_API_KEY is not set")

        aggregate = await BB_AGGREGATE_SENTENCES("elevenlabs")

        return build_elevenlabs_tts(
            ElevenLabsConfig(
                api_key=(
                    ELEVENLABS_INDIAN_RESIDENCY_API_KEY
                    if use_indian_residency
                    else ELEVENLABS_API_KEY
                )
                or "",
                url=(
                    ELEVENLABS_INDIAN_RESIDENCY_WEBSOCKET_URL
                    if use_indian_residency
                    else "wss://api.elevenlabs.io"
                ),
                voice_id=voice_config.voice_id or "",
                model=voice_config.model or "eleven_flash_v2_5",
                speed=voice_config.speed or 1.0,
                language=_parse_language(voice_config.language, Language.EN_IN),
                aggregate_sentences=aggregate,
                enable_ssml_parsing=bool(voice_config.enable_ssml_parsing),
                text_filters=text_filters,
            )
        )

    elif provider == "cartesia":
        if not CARTESIA_API_KEY:
            raise ValueError("CARTESIA_API_KEY is required for Cartesia TTS")

        aggregate = await BB_AGGREGATE_SENTENCES("cartesia")

        generation_config = GenerationConfig(
            volume=voice_config.volume or 1.5,
            speed=voice_config.speed or 1.0,
            emotion=voice_config.emotion or "neutral",
        )

        return build_cartesia_tts(
            CartesiaConfig(
                api_key=CARTESIA_API_KEY,
                voice_id=voice_config.voice_id or "",
                model=voice_config.model or "sonic-3.5",
                language=_parse_language(voice_config.language),
                generation_config=generation_config,
                aggregate_sentences=aggregate,
                text_filters=text_filters,
            )
        )

    elif provider == "sarvam":
        if not SARVAM_API_KEY:
            raise ValueError("SARVAM_API_KEY is required for Sarvam TTS")

        enable_preprocessing = await BB_SARVAM_TTS_ENABLE_PREPROCESSING()

        return build_sarvam_tts(
            SarvamTTSConfig(
                api_key=SARVAM_API_KEY,
                model=voice_config.model or "bulbul:v3",
                voice_id=voice_config.voice_id or "shreya",
                language_code=voice_config.language or "en-IN",
                pitch=voice_config.pitch or 0.0,
                pace=voice_config.speed or 0.9,
                enable_preprocessing=enable_preprocessing,
                text_filters=text_filters,
            )
        )

    elif provider == "gemini":
        if not GOOGLE_CREDENTIALS_JSON:
            raise ValueError("GOOGLE_CREDENTIALS_JSON is required for Gemini TTS")

        return await build_gemini_tts(
            GeminiConfig(
                voice_id=voice_config.voice_id or "Kore",
                model=voice_config.model,  # None → build_gemini_tts resolves via BB_GEMINI_TTS_MODEL()
                language=_parse_language(voice_config.language, Language.EN_IN),
                style_prompt=getattr(voice_config, "style_prompt", None),
                credentials=GOOGLE_CREDENTIALS_JSON,
                text_filters=text_filters,
            )
        )

    elif provider == "google":
        if not GOOGLE_CREDENTIALS_JSON:
            raise ValueError("GOOGLE_CREDENTIALS_JSON is required for Google TTS")

        # Chirp 3 HD: the voice name (e.g. en-IN-Chirp3-HD-Despina) encodes both
        # the model and locale, so there is no model field. Language should match
        # the voice's locale prefix; default to EN_IN.
        return build_google_tts(
            GoogleConfig(
                voice_id=voice_config.voice_id or "en-IN-Chirp3-HD-Despina",
                language=_parse_language(voice_config.language, Language.EN_IN),
                credentials=GOOGLE_CREDENTIALS_JSON,
                text_filters=text_filters,
            )
        )

    elif provider == "soniox":
        if not SONIOX_API_KEY:
            raise ValueError("SONIOX_API_KEY is required for Soniox TTS")

        aggregate = await BB_AGGREGATE_SENTENCES("soniox")

        return build_soniox_tts(
            SonioxTTSConfig(
                api_key=SONIOX_API_KEY,
                voice=voice_config.voice_id or "Priya",
                model=voice_config.model or "tts-rt-v1",
                language=_parse_language(voice_config.language, Language.EN),
                aggregate_sentences=aggregate,
                text_filters=text_filters,
            )
        )

    else:
        raise ValueError(f"Unsupported TTS provider: {provider}")


async def generate_audio(
    text: str,
    voice_config: TTSConfig | None = None,
    configurations: ConfigurationModel | None = None,
) -> bytes:
    """Synthesize text to audio bytes using the resolved voice configuration.

    Args:
        text: The text to synthesize
        voice_config: Resolved TTSConfig. If None, resolves from configurations or defaults.
        configurations: Template configuration model (used to extract tts_configuration if not provided directly).

    Returns:
        Audio bytes in mulaw format (8kHz, mono) ready to send via Twilio
    """
    if not voice_config and configurations:
        voice_config = configurations.tts_configuration

    overrides = configurations.tts_configuration_overrides if configurations else None
    resolved = await resolve_voice_config(voice_config, overrides)
    provider = resolved.provider.value

    # Batch synth calls the provider API directly, bypassing the pipecat TTS
    # service (and its EmojiTextFilter), so strip emoji here too. The caller
    # stores the display text separately — the greeting bubble keeps its emoji.
    if await BB_STRIP_EMOJIS_FROM_TTS():
        text = strip_emojis(text)

    # Route greetings/IVR through the DragonTTS caching proxy (same rule as the
    # live path in get_tts_service): legacy provider="dragontts" always, or an
    # upstream with enable_tts_caching on AND DragonTTS healthy (synthesize
    # model "<provider>:<model>"). When DragonTTS is down, enable_tts_caching
    # greetings synthesize via the upstream directly.
    if provider == "dragontts" or (
        provider != "dragontts"
        and resolved.enable_tts_caching is True
        and await is_dragontts_healthy()
    ):
        if provider != "dragontts":
            if not resolved.model:
                raise ValueError("enable_tts_caching requires a model")
            resolved = resolved.model_copy(
                update={
                    "provider": TTSProvider.DRAGONTTS,
                    "model": f"{provider}:{resolved.model}",
                }
            )
        return await _generate_dragontts_audio(text=text, resolved=resolved)

    if provider == "sarvam":
        audio_data = await _generate_sarvam_audio(
            text=text,
            voice_id=resolved.voice_id,
            model=resolved.model,
            language=resolved.language,
            speed=resolved.speed,
            pitch=resolved.pitch,
        )
        input_format = "raw"
    elif provider == "elevenlabs":
        use_indian_residency = await BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY()
        audio_data = await _generate_elevenlabs_audio(
            text=text,
            voice_id=resolved.voice_id,
            model_id=resolved.model,
            use_indian_residency=use_indian_residency,
        )
        input_format = "ulaw"
    elif provider == "cartesia":
        audio_data = await _generate_cartesia_audio(
            text=text,
            voice_id=resolved.voice_id,
            model=resolved.model,
        )
        input_format = "raw"
    elif provider == "gemini":
        audio_data = await _generate_gemini_audio(
            text=text,
            voice_id=resolved.voice_id,
            model=resolved.model,
            language=resolved.language,
            style_prompt=getattr(resolved, "style_prompt", None),
        )
        # _generate_gemini_audio already downsamples to 16 kHz PCM
        input_format = "raw"
    elif provider == "google":
        audio_data = await _generate_google_audio(
            text=text,
            voice_id=resolved.voice_id,
            language=resolved.language,
        )
        # _generate_google_audio already downsamples to 16 kHz PCM
        input_format = "raw"
    elif provider == "soniox":
        audio_data = await _generate_soniox_audio(
            text=text,
            voice=resolved.voice_id,
            model=resolved.model,
            language=resolved.language,
        )
        input_format = "raw"
    else:
        raise ValueError(f"Unsupported TTS provider: {provider}")

    mulaw_audio = convert_to_mulaw(audio_data, input_format=input_format)
    return mulaw_audio

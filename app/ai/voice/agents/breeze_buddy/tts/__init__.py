"""TTS service utilities for Breeze Buddy voice agent."""

from pipecat.services.cartesia.tts import GenerationConfig
from pipecat.transcriptions.language import Language

from app.ai.voice.agents.breeze_buddy.processors.emoji_text_filter import (
    EmojiTextFilter,
    strip_emoji,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    TTSConfig,
    TTSProvider,
)
from app.ai.voice.agents.breeze_buddy.utils.common import convert_to_mulaw
from app.ai.voice.tts import (
    CartesiaConfig,
    ElevenLabsConfig,
    GeminiConfig,
    SarvamTTSConfig,
    build_cartesia_tts,
    build_elevenlabs_tts,
    build_gemini_tts,
    build_sarvam_tts,
)
from app.ai.voice.tts.cartesia import _generate_cartesia_audio
from app.ai.voice.tts.elevenlabs import _generate_elevenlabs_audio
from app.ai.voice.tts.gemini import _generate_gemini_audio
from app.ai.voice.tts.sarvam import _generate_sarvam_audio
from app.core.config.dynamic import (
    BB_AGGREGATE_SENTENCES,
    BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY,
    BB_SARVAM_TTS_ENABLE_PREPROCESSING,
    BB_TTS_SERVICE,
    BB_VOICE_PROVIDER_DEFAULTS,
)
from app.core.config.static import (
    CARTESIA_API_KEY,
    ELEVENLABS_API_KEY,
    ELEVENLABS_INDIAN_RESIDENCY_API_KEY,
    ELEVENLABS_INDIAN_RESIDENCY_WEBSOCKET_URL,
    GOOGLE_CREDENTIALS_JSON,
    SARVAM_API_KEY,
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

    logger.info(
        f"Building TTS service: provider={provider}, voice_id={voice_config.voice_id}, "
        f"model={voice_config.model}, speed={voice_config.speed}, language={voice_config.language}"
    )

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
                text_filters=[EmojiTextFilter()],
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
                model=voice_config.model or "sonic-3",
                language=_parse_language(voice_config.language),
                generation_config=generation_config,
                aggregate_sentences=aggregate,
                text_filters=[EmojiTextFilter()],
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
                text_filters=[EmojiTextFilter()],
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
                text_filters=[EmojiTextFilter()],
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

    # Strip emoji before sending to any provider — TTS APIs either speak the
    # codepoint name aloud or produce garbled audio when emoji are present.
    text = strip_emoji(text)

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
    else:
        raise ValueError(f"Unsupported TTS provider: {provider}")

    mulaw_audio = convert_to_mulaw(audio_data, input_format=input_format)
    return mulaw_audio

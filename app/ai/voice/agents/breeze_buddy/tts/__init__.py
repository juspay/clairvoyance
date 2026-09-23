"""TTS service utilities for Breeze Buddy voice agent."""

from typing import Optional

from pipecat.services.cartesia.tts import GenerationConfig
from pipecat.transcriptions.language import Language

from app.ai.voice.agents.breeze_buddy.provider_credentials import (
    Accounts,
    GcpAccount,
    KeyAccount,
    unwrap_dragontts,
)
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
    BB_SARVAM_TTS_ENABLE_PREPROCESSING,
    BB_STRIP_EMOJIS_FROM_TTS,
    BB_TTS_SERVICE,
    BB_VOICE_PROVIDER_DEFAULTS,
    DRAGONTTS_URL,
)
from app.core.logger import logger

_VOICE_CONFIG_FIELDS = (
    "credential_id",
    "voice_id",
    "model",
    "language",
    "speed",
    "stability",
    "similarity_boost",
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

    # A DragonTTS voice WITH an account is the nested provider's voice: the
    # unwrap happens once, here, after the merge — so whichever config won
    # (override, template, payload) is the one whose account is checked
    # against the provider that really synthesizes (review, 24 Sep 2026).
    return unwrap_dragontts(TTSConfig(provider=effective_config.provider, **merged))


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


async def get_tts_service(
    voice_config: TTSConfig,
    accounts: Optional[Accounts] = None,
):
    """Build a TTS service from a resolved TTSConfig.

    ``accounts`` is the call's account resolver (provider_credentials): the
    voice's own row when it names one, else the environment's key. A voice
    with an account is synthesized by its provider directly — never through
    the DragonTTS proxy, which holds its own keys and would bill its own
    account (resolve_voice_config already unwrapped such a voice).
    """
    voice_config = unwrap_dragontts(voice_config)
    provider = voice_config.provider.value
    resolver = accounts or Accounts()

    # Emoji stripping applies to EVERY provider/flow, DragonTTS included.
    # pipecat runs these filters only on the string sent to the TTS provider
    # (incl. TTSSpeakFrame used by the widget stream mode + fillers); the
    # transcript frame keeps its emoji. Parity matters here: without the
    # filter, raw emoji reaches DragonTTS's NESTED provider (e.g. Gemini),
    # and Vertex content-policy rejections of the bot's speech are the
    # alert catalog's #1 live TTS failure (B3.1, 93/3d).
    text_filters: list = []
    if await BB_STRIP_EMOJIS_FROM_TTS():
        text_filters.append(EmojiTextFilter())

    # Route through the DragonTTS caching proxy when the template selects it
    # directly (legacy provider="dragontts") OR opts in via enable_tts_caching
    # AND DragonTTS is currently healthy. When DragonTTS is down the health flag
    # is "0", so enable_tts_caching templates fall through to their upstream
    # provider directly (graceful — calls work, just uncached). Legacy
    # provider="dragontts" is intentionally not health-gated.
    if not voice_config.credential_id and (
        provider == "dragontts"
        or (voice_config.enable_tts_caching is True and await is_dragontts_healthy())
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
                text_filters=text_filters,
            )
        )

    logger.info(
        f"Building TTS service: provider={provider}, voice_id={voice_config.voice_id}, "
        f"model={voice_config.model}, speed={voice_config.speed}, language={voice_config.language}, "
        f"enable_ssml_parsing={voice_config.enable_ssml_parsing}, stability={voice_config.stability}, similarity_boost={voice_config.similarity_boost}"
    )

    # The voice's account: key and host together (provider_credentials).
    account = await resolver.get(voice_config)

    if provider == "elevenlabs":
        # The account carries the cluster it lives on — the deployment's
        # (BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY, India by default), for a
        # row's key and the env key alike (ruled 24 Sep 2026).
        assert isinstance(account, KeyAccount)
        api_key = account.api_key
        url = account.endpoint or "wss://api.elevenlabs.io"

        aggregate = await BB_AGGREGATE_SENTENCES("elevenlabs")

        return build_elevenlabs_tts(
            ElevenLabsConfig(
                api_key=api_key,
                url=url,
                voice_id=voice_config.voice_id or "",
                model=voice_config.model or "eleven_flash_v2_5",
                speed=voice_config.speed or 1.0,
                stability=voice_config.stability,
                similarity_boost=voice_config.similarity_boost,
                language=_parse_language(voice_config.language, Language.EN_IN),
                aggregate_sentences=aggregate,
                enable_ssml_parsing=bool(voice_config.enable_ssml_parsing),
                text_filters=text_filters,
            )
        )

    elif provider == "cartesia":
        assert isinstance(account, KeyAccount)
        api_key = account.api_key

        aggregate = await BB_AGGREGATE_SENTENCES("cartesia")

        generation_config = GenerationConfig(
            volume=voice_config.volume or 1.5,
            speed=voice_config.speed or 1.0,
            emotion=voice_config.emotion or "neutral",
        )

        return build_cartesia_tts(
            CartesiaConfig(
                api_key=api_key,
                voice_id=voice_config.voice_id or "",
                model=voice_config.model or "sonic-3.5",
                language=_parse_language(voice_config.language),
                generation_config=generation_config,
                aggregate_sentences=aggregate,
                text_filters=text_filters,
            )
        )

    elif provider == "sarvam":
        assert isinstance(account, KeyAccount)
        api_key = account.api_key

        enable_preprocessing = await BB_SARVAM_TTS_ENABLE_PREPROCESSING()

        return build_sarvam_tts(
            SarvamTTSConfig(
                api_key=api_key,
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
        assert isinstance(account, GcpAccount)
        credentials_json = account.credentials_json

        return await build_gemini_tts(
            GeminiConfig(
                voice_id=voice_config.voice_id or "Kore",
                model=voice_config.model,  # None → build_gemini_tts resolves via BB_GEMINI_TTS_MODEL()
                language=_parse_language(voice_config.language, Language.EN_IN),
                style_prompt=getattr(voice_config, "style_prompt", None),
                credentials=credentials_json,
                text_filters=text_filters,
            )
        )

    elif provider == "google":
        assert isinstance(account, GcpAccount)
        credentials_json = account.credentials_json

        # Chirp 3 HD: the voice name (e.g. en-IN-Chirp3-HD-Despina) encodes both
        # the model and locale, so there is no model field. Language should match
        # the voice's locale prefix; default to EN_IN.
        return build_google_tts(
            GoogleConfig(
                voice_id=voice_config.voice_id or "en-IN-Chirp3-HD-Despina",
                language=_parse_language(voice_config.language, Language.EN_IN),
                credentials=credentials_json,
                text_filters=text_filters,
            )
        )

    elif provider == "soniox":
        assert isinstance(account, KeyAccount)
        api_key = account.api_key

        aggregate = await BB_AGGREGATE_SENTENCES("soniox")

        return build_soniox_tts(
            SonioxTTSConfig(
                api_key=api_key,
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
    accounts: Optional[Accounts] = None,
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

    logger.info(
        f"TTS resolved config: provider={provider}, "
        f"voice_id={resolved.voice_id}, model={resolved.model}, "
        f"language={resolved.language}, speed={resolved.speed}, volume={resolved.volume}"
    )

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
    if not resolved.credential_id and (
        provider == "dragontts"
        or (
            provider != "dragontts"
            and resolved.enable_tts_caching is True
            and await is_dragontts_healthy()
        )
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

    # The voice's account — a row's or the environment's — key and host
    # together (provider_credentials.Accounts), the same answer the live
    # path gets for the same voice.
    account = await (accounts or Accounts()).get(resolved)
    account_key = getattr(account, "api_key", None)
    account_credentials_json = getattr(account, "credentials_json", None)

    if provider == "sarvam":
        audio_data = await _generate_sarvam_audio(
            text=text,
            voice_id=resolved.voice_id,
            model=resolved.model,
            language=resolved.language,
            speed=resolved.speed,
            pitch=resolved.pitch,
            api_key=account_key,
        )
        input_format = "raw"
    elif provider == "elevenlabs":
        # The account's host decides the cluster, as on the live path.
        use_indian_residency = (
            getattr(account, "endpoint", None) or ""
        ) != "wss://api.elevenlabs.io"
        audio_data = await _generate_elevenlabs_audio(
            text=text,
            voice_id=resolved.voice_id,
            model_id=resolved.model,
            use_indian_residency=use_indian_residency,
            speed=resolved.speed,
            stability=resolved.stability,
            similarity_boost=resolved.similarity_boost,
            language=(
                _parse_language(resolved.language) if resolved.language else None
            ),
            api_key=account_key,
        )
        input_format = "ulaw"
    elif provider == "cartesia":
        audio_data = await _generate_cartesia_audio(
            text=text,
            voice_id=resolved.voice_id,
            model=resolved.model,
            api_key=account_key,
        )
        input_format = "raw"
    elif provider == "gemini":
        audio_data = await _generate_gemini_audio(
            text=text,
            voice_id=resolved.voice_id,
            model=resolved.model,
            language=resolved.language,
            style_prompt=getattr(resolved, "style_prompt", None),
            credentials_json=account_credentials_json,
        )
        # _generate_gemini_audio already downsamples to 16 kHz PCM
        input_format = "raw"
    elif provider == "google":
        audio_data = await _generate_google_audio(
            text=text,
            voice_id=resolved.voice_id,
            language=resolved.language,
            credentials_json=account_credentials_json,
        )
        # _generate_google_audio already downsamples to 16 kHz PCM
        input_format = "raw"
    elif provider == "soniox":
        audio_data = await _generate_soniox_audio(
            text=text,
            voice=resolved.voice_id,
            model=resolved.model,
            language=resolved.language,
            api_key=account_key,
        )
        input_format = "raw"
    else:
        raise ValueError(f"Unsupported TTS provider: {provider}")

    mulaw_audio = convert_to_mulaw(audio_data, input_format=input_format)
    return mulaw_audio

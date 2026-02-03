"""TTS service utilities for Breeze Buddy voice agent."""

from pipecat.services.cartesia.tts import GenerationConfig
from pipecat.transcriptions.language import Language

from app.ai.voice.agents.breeze_buddy.utils.common import convert_to_mulaw
from app.ai.voice.tts import (
    CartesiaConfig,
    ElevenLabsConfig,
    SarvamTTSConfig,
    build_cartesia_tts,
    build_elevenlabs_tts,
    build_sarvam_tts,
)
from app.ai.voice.tts.cartesia import _generate_cartesia_audio
from app.ai.voice.tts.elevenlabs import _generate_elevenlabs_audio
from app.ai.voice.tts.sarvam import _generate_sarvam_audio
from app.ai.voice.tts.tts_health_observer import TTS_SWAP_MAP
from app.core.config.dynamic import (
    BB_CARTESIA_AGGREGATE_SENTENCES,
    BB_CARTESIA_GENERATION_EMOTION,
    BB_CARTESIA_GENERATION_SPEED,
    BB_CARTESIA_GENERATION_VOLUME,
    BB_CARTESIA_LANGUAGE,
    BB_CARTESIA_MODEL,
    BB_CARTESIA_VOICE_ID,
    BB_SARVAM_TTS_ENABLE_PREPROCESSING,
    BB_SARVAM_TTS_LANGUAGE_CODE,
    BB_SARVAM_TTS_MODEL,
    BB_SARVAM_TTS_PACE,
    BB_SARVAM_TTS_PITCH,
    BB_SARVAM_TTS_VOICE_ID,
    BB_TTS_FAILED_PROVIDER,
    BB_TTS_FALLBACK_ENABLED,
    BB_TTS_SERVICE,
)
from app.core.config.static import (
    CARTESIA_API_KEY,
    ELEVENLABS_API_KEY,
    ELEVENLABS_BB_VOICE_ID,
    ELEVENLABS_MODEL_ID,
    ELEVENLABS_VOICE_SPEED,
    SARVAM_API_KEY,
)
from app.core.logger import logger


async def get_cartesia_tts_service(voice_id: str | None = None):
    """
    Returns a Cartesia TTS service instance based on the Breeze Buddy configuration.

    Args:
        voice_id: Optional custom voice ID from template configuration.
                          If provided, overrides the default BB_CARTESIA_VOICE_ID.
    """
    bb_cartesia_voice_id = voice_id or await BB_CARTESIA_VOICE_ID()
    bb_cartesia_model = await BB_CARTESIA_MODEL()
    bb_cartesia_language = await BB_CARTESIA_LANGUAGE()
    bb_cartesia_generation_volume = await BB_CARTESIA_GENERATION_VOLUME()
    bb_cartesia_generation_speed = await BB_CARTESIA_GENERATION_SPEED()
    bb_cartesia_generation_emotion = await BB_CARTESIA_GENERATION_EMOTION()
    bb_cartesia_aggregate_sentences = await BB_CARTESIA_AGGREGATE_SENTENCES()

    generation_config = GenerationConfig(
        volume=bb_cartesia_generation_volume,
        speed=bb_cartesia_generation_speed,
        emotion=bb_cartesia_generation_emotion,
    )

    # Map language code string to Language enum
    # The language code from config is like "en", "hi", etc.
    # We'll use Language.EN as default and let Cartesia handle the language string directly
    language = Language.EN
    if bb_cartesia_language:
        # Try to get the language enum by uppercasing the code
        try:
            language = Language[bb_cartesia_language.upper().replace("-", "_")]
        except KeyError:
            logger.warning(
                f"Language code '{bb_cartesia_language}' not found in Language enum, using EN"
            )

    return build_cartesia_tts(
        CartesiaConfig(
            api_key=CARTESIA_API_KEY,
            voice_id=bb_cartesia_voice_id,
            model=bb_cartesia_model,
            language=language,
            generation_config=generation_config,
            aggregate_sentences=bb_cartesia_aggregate_sentences,
        )
    )


async def get_sarvam_tts_service():
    """
    Returns a Sarvam TTS service instance based on the Breeze Buddy configuration.
    """
    bb_sarvam_tts_model = await BB_SARVAM_TTS_MODEL()
    bb_sarvam_tts_voice_id = await BB_SARVAM_TTS_VOICE_ID()
    bb_sarvam_tts_language_code = await BB_SARVAM_TTS_LANGUAGE_CODE()
    bb_sarvam_tts_pitch = await BB_SARVAM_TTS_PITCH()
    bb_sarvam_tts_pace = await BB_SARVAM_TTS_PACE()
    bb_sarvam_tts_enable_preprocessing = await BB_SARVAM_TTS_ENABLE_PREPROCESSING()

    return build_sarvam_tts(
        SarvamTTSConfig(
            api_key=SARVAM_API_KEY,
            model=bb_sarvam_tts_model,
            voice_id=bb_sarvam_tts_voice_id,
            language_code=bb_sarvam_tts_language_code,
            pitch=bb_sarvam_tts_pitch,
            pace=bb_sarvam_tts_pace,
            enable_preprocessing=bb_sarvam_tts_enable_preprocessing,
        )
    )


async def get_elevenlabs_tts_service():
    """
    Returns an ElevenLabs TTS service instance based on the Breeze Buddy configuration.
    """
    return build_elevenlabs_tts(
        ElevenLabsConfig(
            api_key=ELEVENLABS_API_KEY,
            voice_id=ELEVENLABS_BB_VOICE_ID,
            model_id=ELEVENLABS_MODEL_ID,
            speed=ELEVENLABS_VOICE_SPEED,
            language=Language.EN_IN,
        )
    )


async def get_tts_service(
    voice_name: str | None = None, mira_voice_id: str | None = None
):
    """
    Returns a TTS service instance based on the environment configuration.

    Supports the following voice names:
    - "sara": Sarvam TTS (multilingual Indian voices)
    - "rhea": ElevenLabs TTS (high-quality English)
    - "mira": Cartesia TTS (customizable with emotions and speed)

    Respects fallback settings:
    - If fallback is enabled and rhea (ElevenLabs) is requested, returns mira (Cartesia)
    - If fallback is enabled and mira (Cartesia) is requested, returns rhea (ElevenLabs)
    """
    # Voice name to provider mapping
    VOICE_TO_PROVIDER = {
        "rhea": "elevenlabs",
        "mira": "cartesia",
        "sara": "sarvam",
    }

    # Check if fallback is active
    fallback_enabled = await BB_TTS_FALLBACK_ENABLED()
    failed_provider = await BB_TTS_FAILED_PROVIDER()

    if voice_name is not None:
        voice_lower = voice_name.lower()
        logger.info(f"TTS voice requested: {voice_name}")

        # Map voice to provider
        requested_provider = VOICE_TO_PROVIDER.get(voice_lower)

        # Apply fallback ONLY if the requested provider is the one currently failing
        if (
            fallback_enabled
            and requested_provider == failed_provider
            and requested_provider in TTS_SWAP_MAP
        ):
            actual_provider = TTS_SWAP_MAP[requested_provider]
            logger.warning(
                f"🔄 TTS fallback triggered for failing service: {voice_name} ({requested_provider}) → {actual_provider}"
            )
        else:
            actual_provider = requested_provider

        # Return appropriate service
        if actual_provider == "sarvam":
            if not SARVAM_API_KEY:
                raise ValueError("SARVAM_API_KEY is required for Sara voice")
            logger.info("Using Sarvam TTS service")
            return await get_sarvam_tts_service()

        elif actual_provider == "elevenlabs":
            if not ELEVENLABS_API_KEY:
                raise ValueError("ELEVENLABS_API_KEY is required for ElevenLabs")
            logger.info("Using ElevenLabs TTS service")
            return await get_elevenlabs_tts_service()

        elif actual_provider == "cartesia":
            if not CARTESIA_API_KEY:
                raise ValueError("CARTESIA_API_KEY is required for Mira voice")

            if mira_voice_id:
                logger.info(
                    f"Using Cartesia TTS service for Mira voice with custom voice_id: {mira_voice_id}"
                )
            else:
                logger.info("Using Cartesia TTS service for Mira voice")
            return await get_cartesia_tts_service(voice_id=mira_voice_id)

        else:
            raise ValueError(f"Unknown voice: {voice_name}")

    # No voice_name specified, use default from config
    logger.info("No TTS voice specified, using default from config")

    # Get configured service and apply fallback if it's the failing one
    configured_service = await BB_TTS_SERVICE()

    if fallback_enabled and configured_service == failed_provider:
        # Swap to alternate provider
        tts_service = TTS_SWAP_MAP.get(configured_service, configured_service)
        logger.warning(
            f"🔄 Default TTS service ({configured_service}) is failing, using fallback: {tts_service}"
        )
    else:
        tts_service = configured_service

    if tts_service == "sarvam":
        if not SARVAM_API_KEY:
            raise ValueError(
                "SARVAM_API_KEY is required when BREEZE_BUDDY_TTS_SERVICE=sarvam"
            )

        logger.info("Using Sarvam TTS service for Breeze Buddy voice")
        return await get_sarvam_tts_service()

    elif tts_service == "elevenlabs":
        if not ELEVENLABS_API_KEY:
            raise ValueError(
                "ELEVENLABS_API_KEY is required when BREEZE_BUDDY_TTS_SERVICE=elevenlabs"
            )

        logger.info("Using ElevenLabs TTS service for Breeze Buddy voice")
        return await get_elevenlabs_tts_service()

    elif tts_service == "cartesia":
        if not CARTESIA_API_KEY:
            raise ValueError(
                "CARTESIA_API_KEY is required when BREEZE_BUDDY_TTS_SERVICE=cartesia"
            )

        if mira_voice_id:
            logger.info(
                f"Using Cartesia TTS service for Breeze Buddy voice with custom voice_id: {mira_voice_id}"
            )
        else:
            logger.info("Using Cartesia TTS service for Breeze Buddy voice")
        return await get_cartesia_tts_service(voice_id=mira_voice_id)

    else:
        raise ValueError(f"Unsupported BREEZE_BUDDY_TTS_SERVICE: {tts_service}")


async def generate_audio(text: str, voice_name: str) -> bytes:
    """
    Synthesize text to audio bytes using the specified TTS voice.

    Args:
        text: The text to synthesize
        voice_name: The TTS voice to use ("sara", "rhea", or "mira")

    Returns:
        Audio bytes in mulaw format (8kHz, mono) ready to send via Twilio

    Raises:
        ValueError: If voice_name is invalid or required API keys are missing
        Exception: If synthesis fails
    """
    voice_name_lower = voice_name.lower()

    # Map voice to provider
    VOICE_TO_PROVIDER = {
        "rhea": "elevenlabs",
        "mira": "cartesia",
        "sara": "sarvam",
    }
    PROVIDER_TO_VOICE = {
        "elevenlabs": "rhea",
        "cartesia": "mira",
        "sarvam": "sara",
    }

    requested_provider = VOICE_TO_PROVIDER.get(voice_name_lower)

    # Check for fallback
    fallback_enabled = await BB_TTS_FALLBACK_ENABLED()
    failed_provider = await BB_TTS_FAILED_PROVIDER()

    # Apply fallback swap if needed
    if (
        fallback_enabled
        and requested_provider == failed_provider
        and requested_provider in TTS_SWAP_MAP
    ):
        actual_provider = TTS_SWAP_MAP[requested_provider]
        voice_to_use = PROVIDER_TO_VOICE.get(actual_provider, voice_name_lower)
        logger.warning(
            f"🔄 Greeting TTS fallback: {voice_name_lower} ({requested_provider}) → {voice_to_use} ({actual_provider})"
        )
    else:
        voice_to_use = voice_name_lower

    if voice_to_use == "sara":
        audio_data = await _generate_sarvam_audio(text)
        input_format = "raw"
    elif voice_to_use == "rhea":
        audio_data = await _generate_elevenlabs_audio(text)
        input_format = "ulaw"
    elif voice_to_use == "mira":
        audio_data = await _generate_cartesia_audio(text)
        input_format = "raw"
    else:
        raise ValueError(
            f"Invalid voice_name: {voice_name}. Must be 'sara', 'rhea', or 'mira'"
        )

    # Convert to Twilio-compatible format (8kHz, mono, mulaw)
    mulaw_audio = convert_to_mulaw(audio_data, input_format=input_format)
    return mulaw_audio

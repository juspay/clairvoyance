"""TTS service utilities for Breeze Buddy voice agent."""

from pipecat.services.cartesia.tts import GenerationConfig
from pipecat.transcriptions.language import Language

from app.ai.voice.agents.breeze_buddy.template.types import (
    CartesiaVoiceConfiguration,
    ConfigurationModel,
    ElevenLabsVoiceConfiguration,
)
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
from app.core.config.dynamic import (
    BB_CARTESIA_AGGREGATE_SENTENCES,
    BB_CARTESIA_GENERATION_EMOTION,
    BB_CARTESIA_GENERATION_SPEED,
    BB_CARTESIA_GENERATION_VOLUME,
    BB_CARTESIA_LANGUAGE,
    BB_CARTESIA_MODEL,
    BB_CARTESIA_VOICE_ID,
    BB_ELEVENLABS_MODEL_ID,
    BB_ELEVENLABS_VOICE_ID,
    BB_ELEVENLABS_VOICE_SPEED,
    BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY,
    BB_SARVAM_TTS_ENABLE_PREPROCESSING,
    BB_SARVAM_TTS_LANGUAGE_CODE,
    BB_SARVAM_TTS_MODEL,
    BB_SARVAM_TTS_PACE,
    BB_SARVAM_TTS_PITCH,
    BB_SARVAM_TTS_VOICE_ID,
    BB_TTS_SERVICE,
)
from app.core.config.static import (
    CARTESIA_API_KEY,
    ELEVENLABS_API_KEY,
    ELEVENLABS_INDIAN_RESIDENCY_API_KEY,
    ELEVENLABS_INDIAN_RESIDENCY_WEBSOCKET_URL,
    SARVAM_API_KEY,
)
from app.core.logger import logger


async def get_cartesia_tts_service(
    voice_id: str | None = None,
    cartesia_config: CartesiaVoiceConfiguration | None = None,
):
    """
    Returns a Cartesia TTS service instance based on the Breeze Buddy configuration.

    Template-level values (if provided) override global Redis defaults.

    Args:
        voice_id: DEPRECATED - Optional custom voice ID from legacy template configuration.
                  Use cartesia_config.voice_id instead.
        cartesia_config: Template-level Cartesia voice configuration. Values specified here
                        override global Redis defaults. If None or specific fields are None,
                        falls back to Redis configuration.
    """
    # Load global Redis defaults
    default_voice_id = await BB_CARTESIA_VOICE_ID()
    default_model = await BB_CARTESIA_MODEL()
    default_language = await BB_CARTESIA_LANGUAGE()
    default_volume = await BB_CARTESIA_GENERATION_VOLUME()
    default_speed = await BB_CARTESIA_GENERATION_SPEED()
    default_emotion = await BB_CARTESIA_GENERATION_EMOTION()
    default_aggregate_sentences = await BB_CARTESIA_AGGREGATE_SENTENCES()

    # Template overrides Redis (priority: cartesia_config.voice_id > legacy voice_id > default)
    final_voice_id = default_voice_id
    if cartesia_config and cartesia_config.voice_id:
        final_voice_id = cartesia_config.voice_id
    elif voice_id:
        final_voice_id = voice_id

    # Template overrides for generation parameters (or fall back to defaults)
    final_volume = (
        cartesia_config.volume
        if cartesia_config and cartesia_config.volume is not None
        else default_volume
    )
    final_speed = (
        cartesia_config.speed
        if cartesia_config and cartesia_config.speed is not None
        else default_speed
    )
    final_emotion = (
        cartesia_config.emotion
        if cartesia_config and cartesia_config.emotion is not None
        else default_emotion
    )
    final_language_code = (
        cartesia_config.language
        if cartesia_config and cartesia_config.language is not None
        else default_language
    )

    # Build generation config with final values
    generation_config = GenerationConfig(
        volume=final_volume,
        speed=final_speed,
        emotion=final_emotion,
    )

    # Map language code string to Language enum
    # The language code from config is like "en", "hi", etc.
    # We'll use Language.EN as default and let Cartesia handle the language string directly
    language = Language.EN
    if final_language_code:
        # Try to get the language enum by uppercasing the code
        try:
            language = Language[final_language_code.upper().replace("-", "_")]
        except KeyError:
            logger.warning(
                f"Language code '{final_language_code}' not found in Language enum, using EN"
            )

    if not CARTESIA_API_KEY:
        raise ValueError("CARTESIA_API_KEY is required for Cartesia TTS")

    logger.info(
        f"Building Cartesia TTS with: voice_id={final_voice_id}, "
        f"volume={final_volume}, speed={final_speed}, emotion={final_emotion}, language={final_language_code}"
    )

    return build_cartesia_tts(
        CartesiaConfig(
            api_key=CARTESIA_API_KEY,
            voice_id=final_voice_id,
            model=default_model,
            language=language,
            generation_config=generation_config,
            aggregate_sentences=default_aggregate_sentences,
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

    if not SARVAM_API_KEY:
        raise ValueError("SARVAM_API_KEY is not set")

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


async def get_elevenlabs_tts_service(
    elevenlabs_config: ElevenLabsVoiceConfiguration | None = None,
):
    """
    Returns an ElevenLabs TTS service instance based on the Breeze Buddy configuration.

    Template-level values (if provided) override global Redis defaults.

    Args:
        elevenlabs_config: Template-level ElevenLabs voice configuration. Values specified here
                          override global Redis defaults. If None or specific fields are None,
                          falls back to Redis configuration.
    """
    use_indian_residency = await BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY()
    if use_indian_residency and not ELEVENLABS_INDIAN_RESIDENCY_API_KEY:
        raise ValueError(
            "ELEVENLABS_INDIAN_RESIDENCY_API_KEY is required when BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY is True"
        )

    if not use_indian_residency and not ELEVENLABS_API_KEY:
        raise ValueError("ELEVENLABS_API_KEY is not set")

    # Load global Redis defaults
    default_voice_id = await BB_ELEVENLABS_VOICE_ID()
    default_model_id = await BB_ELEVENLABS_MODEL_ID()
    default_speed = await BB_ELEVENLABS_VOICE_SPEED()

    # Template overrides Redis defaults
    final_voice_id = (
        elevenlabs_config.voice_id
        if elevenlabs_config and elevenlabs_config.voice_id
        else default_voice_id
    )
    final_model_id = (
        elevenlabs_config.model_id
        if elevenlabs_config and elevenlabs_config.model_id
        else default_model_id
    )
    final_speed = (
        elevenlabs_config.speed
        if elevenlabs_config and elevenlabs_config.speed is not None
        else default_speed
    )

    # Map language code string to Language enum
    language = Language.EN_IN
    if elevenlabs_config and elevenlabs_config.language:
        try:
            language = Language[elevenlabs_config.language.upper().replace("-", "_")]
        except KeyError:
            logger.warning(
                f"Language code '{elevenlabs_config.language}' not found in Language enum, using EN_IN"
            )

    logger.info(
        f"Building ElevenLabs TTS with: voice_id={final_voice_id}, "
        f"model_id={final_model_id}, speed={final_speed}, language={language}"
    )

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
            voice_id=final_voice_id,
            model_id=final_model_id,
            speed=final_speed,
            language=language,
        )
    )


async def get_tts_service(
    voice_name: str | None = None,
    mira_voice_id: str | None = None,
    cartesia_voice_configurations: CartesiaVoiceConfiguration | None = None,
    elevenlabs_voice_configurations: ElevenLabsVoiceConfiguration | None = None,
):
    """
    Returns a TTS service instance based on the environment configuration.

    Supports the following voice names:
    - "sara": Sarvam TTS (multilingual Indian voices)
    - "rhea": ElevenLabs TTS (high-quality English)
    - "mira": Cartesia TTS (customizable with emotions and speed)

    Args:
        voice_name: The TTS voice to use ("sara", "rhea", or "mira")
        mira_voice_id: DEPRECATED - Optional custom Cartesia voice ID from legacy template configuration.
                      Use cartesia_voice_configurations.voice_id instead.
        cartesia_voice_configurations: Template-level Cartesia voice configuration (volume, speed, emotion, language).
                                       Only used when voice_name is "mira" or tts_service is "cartesia".
        elevenlabs_voice_configurations: Template-level ElevenLabs voice configuration (voice_id, model_id, speed, language).
                                         Only used when voice_name is "rhea" or tts_service is "elevenlabs".
    """

    if voice_name is not None:
        logger.info(f"Using specified TTS voice: {voice_name}")
        if voice_name.lower() == "sara":
            if not SARVAM_API_KEY:
                raise ValueError("SARVAM_API_KEY is required for Sara voice")

            logger.info("Using Sarvam TTS service for Sara voice")
            return await get_sarvam_tts_service()

        elif voice_name.lower() == "rhea":
            logger.info("Using ElevenLabs TTS service for Rhea voice")
            return await get_elevenlabs_tts_service(
                elevenlabs_config=elevenlabs_voice_configurations,
            )

        elif voice_name.lower() == "mira":
            if not CARTESIA_API_KEY:
                raise ValueError("CARTESIA_API_KEY is required for Mira voice")

            logger.info("Using Cartesia TTS service for Mira voice")
            return await get_cartesia_tts_service(
                voice_id=mira_voice_id,
                cartesia_config=cartesia_voice_configurations,
            )
    else:
        logger.info("No TTS voice specified, using default from config")

    tts_service = await BB_TTS_SERVICE()
    if tts_service == "sarvam":
        if not SARVAM_API_KEY:
            raise ValueError(
                "SARVAM_API_KEY is required when BREEZE_BUDDY_TTS_SERVICE=sarvam"
            )

        logger.info("Using Sarvam TTS service for Breeze Buddy voice")
        return await get_sarvam_tts_service()

    elif tts_service == "elevenlabs":
        logger.info("Using ElevenLabs TTS service for Breeze Buddy voice")
        return await get_elevenlabs_tts_service(
            elevenlabs_config=elevenlabs_voice_configurations,
        )

    elif tts_service == "cartesia":
        if not CARTESIA_API_KEY:
            raise ValueError(
                "CARTESIA_API_KEY is required when BREEZE_BUDDY_TTS_SERVICE=cartesia"
            )

        logger.info("Using Cartesia TTS service for Breeze Buddy voice")
        return await get_cartesia_tts_service(
            voice_id=mira_voice_id,
            cartesia_config=cartesia_voice_configurations,
        )

    else:
        raise ValueError(f"Unsupported BREEZE_BUDDY_TTS_SERVICE: {tts_service}")


async def generate_audio(
    text: str, voice_name: str, configurations: ConfigurationModel | None = None
) -> bytes:
    """
    Synthesize text to audio bytes using the specified TTS voice.

    Args:
        text: The text to synthesize
        voice_name: The TTS voice to use ("sara", "rhea", or "mira")
        configurations: Optional configuration model containing voice configurations.
                       For ElevenLabs (rhea): uses voice_id and model_id if provided.
                       For Cartesia (mira): uses voice_id and model if provided.
                       Falls back to default values from static config if None.

    Returns:
        Audio bytes in mulaw format (8kHz, mono) ready to send via Twilio

    Raises:
        ValueError: If voice_name is invalid or required API keys are missing
        Exception: If synthesis fails
    """
    voice_name_lower = voice_name.lower()

    if voice_name_lower == "sara":
        audio_data = await _generate_sarvam_audio(text)
        input_format = "raw"
    elif voice_name_lower == "rhea":
        # Extract ElevenLabs config if available
        elevenlabs_config = (
            configurations.elevenlabs_voice_configurations if configurations else None
        )
        voice_id = (
            elevenlabs_config.voice_id
            if elevenlabs_config and elevenlabs_config.voice_id
            else None
        )
        model_id = (
            elevenlabs_config.model_id
            if elevenlabs_config and elevenlabs_config.model_id
            else None
        )

        # Check if Indian residency should be used (default to True from dynamic config)
        use_indian_residency = await BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY()

        audio_data = await _generate_elevenlabs_audio(
            text=text,
            voice_id=voice_id,
            model_id=model_id,
            use_indian_residency=use_indian_residency,
        )
        input_format = "ulaw"
    elif voice_name_lower == "mira":
        # Extract Cartesia config if available (only voice_id is configurable at template level)
        cartesia_config = (
            configurations.cartesia_voice_configurations if configurations else None
        )
        voice_id = (
            cartesia_config.voice_id
            if cartesia_config and cartesia_config.voice_id
            else None
        )

        audio_data = await _generate_cartesia_audio(
            text=text,
            voice_id=voice_id,
            # model uses default from BB_CARTESIA_MODEL (not configurable at template level)
        )
        input_format = "raw"
    else:
        raise ValueError(
            f"Invalid voice_name: {voice_name}. Must be 'sara', 'rhea', or 'mira'"
        )

    # Convert to Twilio-compatible format (8kHz, mono, mulaw)
    mulaw_audio = convert_to_mulaw(audio_data, input_format=input_format)
    return mulaw_audio

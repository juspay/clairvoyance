from pipecat.services.cartesia.tts import GenerationConfig
from pipecat.transcriptions.language import Language

from app.ai.voice.tts import (
    CartesiaConfig,
    ElevenLabsConfig,
    SarvamTTSConfig,
    build_cartesia_tts,
    build_elevenlabs_tts,
    build_sarvam_tts,
)
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


async def get_cartesia_tts_service():
    """
    Returns a Cartesia TTS service instance based on the Breeze Buddy configuration.
    """
    bb_cartesia_voice_id = await BB_CARTESIA_VOICE_ID()
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


async def get_tts_service(voice_name: str | None = None):
    """
    Returns a TTS service instance based on the environment configuration.

    Supports the following voice names:
    - "sara": Sarvam TTS (multilingual Indian voices)
    - "rhea": ElevenLabs TTS (high-quality English)
    - "mira": Cartesia TTS (customizable with emotions and speed)
    """

    if voice_name is not None:
        logger.info(f"Using specified TTS voice: {voice_name}")
        if voice_name.lower() == "sara":
            if not SARVAM_API_KEY:
                raise ValueError("SARVAM_API_KEY is required for Sara voice")

            logger.info("Using Sarvam TTS service for Sara voice")
            return await get_sarvam_tts_service()

        elif voice_name.lower() == "rhea":
            if not ELEVENLABS_API_KEY:
                raise ValueError("ELEVENLABS_API_KEY is required for Rhea voice")

            logger.info("Using ElevenLabs TTS service for Rhea voice")
            return await get_elevenlabs_tts_service()

        elif voice_name.lower() == "mira":
            if not CARTESIA_API_KEY:
                raise ValueError("CARTESIA_API_KEY is required for Mira voice")

            logger.info("Using Cartesia TTS service for Mira voice")
            return await get_cartesia_tts_service()
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

        logger.info("Using Cartesia TTS service for Breeze Buddy voice")
        return await get_cartesia_tts_service()

    else:
        raise ValueError(f"Unsupported BREEZE_BUDDY_TTS_SERVICE: {tts_service}")

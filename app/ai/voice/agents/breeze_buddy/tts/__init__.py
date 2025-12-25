from pipecat.transcriptions.language import Language

from app.ai.voice.tts import (
    ElevenLabsConfig,
    SarvamTTSConfig,
    build_elevenlabs_tts,
    build_sarvam_tts,
)
from app.core.config.dynamic import (
    BB_SARVAM_TTS_ENABLE_PREPROCESSING,
    BB_SARVAM_TTS_LANGUAGE_CODE,
    BB_SARVAM_TTS_MODEL,
    BB_SARVAM_TTS_PACE,
    BB_SARVAM_TTS_PITCH,
    BB_SARVAM_TTS_VOICE_ID,
    BB_TTS_SERVICE,
)
from app.core.config.static import (
    ELEVENLABS_API_KEY,
    ELEVENLABS_BB_VOICE_ID,
    ELEVENLABS_MODEL_ID,
    ELEVENLABS_VOICE_SPEED,
    SARVAM_API_KEY,
)
from app.core.logger import logger


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

    else:
        raise ValueError(f"Unsupported BREEZE_BUDDY_TTS_SERVICE: {tts_service}")

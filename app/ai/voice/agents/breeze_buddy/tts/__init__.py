from pipecat.transcriptions.language import Language

from app.ai.voice.tts import (
    ElevenLabsConfig,
    SarvamTTSConfig,
    build_elevenlabs_tts,
    build_sarvam_tts,
)
from app.core.config.dynamic import (
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


async def get_tts_service():
    """
    Returns a TTS service instance based on the environment configuration.
    """
    tts_service = await BB_TTS_SERVICE()
    if tts_service == "sarvam":
        if not SARVAM_API_KEY:
            raise ValueError(
                "SARVAM_API_KEY is required when BREEZE_BUDDY_TTS_SERVICE=sarvam"
            )

        # Get Breeze Buddy-specific dynamic config values from Redis
        bb_sarvam_tts_model = await BB_SARVAM_TTS_MODEL()
        bb_sarvam_tts_voice_id = await BB_SARVAM_TTS_VOICE_ID()
        bb_sarvam_tts_language_code = await BB_SARVAM_TTS_LANGUAGE_CODE()
        bb_sarvam_tts_pitch = await BB_SARVAM_TTS_PITCH()
        bb_sarvam_tts_pace = await BB_SARVAM_TTS_PACE()

        return build_sarvam_tts(
            SarvamTTSConfig(
                api_key=SARVAM_API_KEY,
                model=bb_sarvam_tts_model,
                voice_id=bb_sarvam_tts_voice_id,
                language_code=bb_sarvam_tts_language_code,
                pitch=bb_sarvam_tts_pitch,
                pace=bb_sarvam_tts_pace,
            )
        )
    elif tts_service == "elevenlabs":
        if not ELEVENLABS_API_KEY:
            raise ValueError(
                "ELEVENLABS_API_KEY is required when BREEZE_BUDDY_TTS_SERVICE=elevenlabs"
            )
        logger.info("Using ElevenLabs TTS service for Breeze Buddy voice")
        return build_elevenlabs_tts(
            ElevenLabsConfig(
                api_key=ELEVENLABS_API_KEY,
                voice_id=ELEVENLABS_BB_VOICE_ID,
                model_id=ELEVENLABS_MODEL_ID,
                speed=ELEVENLABS_VOICE_SPEED,
                language=Language.EN_IN,
            )
        )
    else:
        raise ValueError(f"Unsupported BREEZE_BUDDY_TTS_SERVICE: {tts_service}")

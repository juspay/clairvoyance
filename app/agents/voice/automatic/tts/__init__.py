from enum import Enum

from app.core.logger import logger
from app.core import config

from pipecat.services.google.tts import GoogleTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transcriptions.language import Language

class TTSService(Enum):
    GOOGLE = "GOOGLE"
    ELEVENLABS = "ELEVENLABS"

def get_tts_service(tts_service: str | None = None):
    """
    Returns a TTS service instance based on the environment configuration.
    """
    service_to_use = tts_service or config.DEFAULT_TTS_SERVICE
    logger.info(f"Initializing TTS service: {service_to_use}")

    if service_to_use == TTSService.ELEVENLABS.value:
        logger.info("Using ElevenLabs TTS service.")
        return ElevenLabsTTSService(
            api_key=config.ELEVENLABS_API_KEY,
            voice_id=config.ELEVENLABS_VOICE_ID,
            model_id=config.ELEVENLABS_MODEL_ID,
            params=ElevenLabsTTSService.InputParams(speed=0.8, language=Language.EN_IN),
        )

    logger.info("Using Google TTS service as default.")
    return GoogleTTSService(
        voice_id="en-IN-Chirp3-HD-Sadaltager",
        params=GoogleTTSService.InputParams(language=Language.EN_IN),
        credentials=config.GOOGLE_CREDENTIALS_JSON
    )
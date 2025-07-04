from app.core.logger import logger
from app.core import config
from app.agents.voice.automatic.types import TTSProvider, VoiceName

from pipecat.services.google.tts import GoogleTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transcriptions.language import Language

def get_tts_service(tts_provider: str | None = None, voice_name: str | None = None):
    """
    Returns a TTS service instance based on the environment configuration.
    """
    logger.info(f"Initializing TTS service: {tts_provider}")

    if tts_provider == TTSProvider.ELEVENLABS.value and voice_name == VoiceName.RHEA.value:
        logger.info("Using ElevenLabs TTS service for RHEA voice.")
        return ElevenLabsTTSService(
            api_key=config.ELEVENLABS_API_KEY,
            voice_id=config.ELEVENLABS_RHEA_VOICE_ID,
            model_id=config.ELEVENLABS_MODEL_ID,
            params=ElevenLabsTTSService.InputParams(speed=0.8, language=Language.EN_IN),
        )
    
    voice_id = config.GOOGLE_BRET_VOICE # Default to BRET
    if tts_provider == TTSProvider.GOOGLE.value:
        if voice_name == VoiceName.MIA.value:
            voice_id = config.GOOGLE_MIA_VOICE
            logger.info(f"Using Google TTS service with MIA voice.")
        else:
            logger.info(f"Using Google TTS service with BRET voice.")
    
    return GoogleTTSService(
        voice_id=voice_id,
        params=GoogleTTSService.InputParams(language=Language.EN_IN),
        credentials=config.GOOGLE_CREDENTIALS_JSON
    )

from app.core.logger import logger
from app.core import config
from app.agents.voice.automatic.types import TTSProvider, VoiceName
from typing import Optional

from pipecat.services.google.tts import GoogleTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transcriptions.language import Language

from app.agents.voice.automatic.services.charts.highlight_filter import HighlightedChartTextFilter

def get_tts_service(
    tts_provider: str | None = None, 
    voice_name: str | None = None,
    session_id: Optional[str] = None,
    enable_chart_text_filter: bool | None = None
):
    """
    Returns a TTS service instance based on the environment configuration.
    
    Args:
        tts_provider: TTS provider type (google/elevenlabs)
        voice_name: Voice name to use
        session_id: Session ID for highlight filtering
    """
    logger.info(f"Initializing TTS service: {tts_provider}")
    
    # Create highlight filter if session context is available
    text_filters = []
    if session_id and enable_chart_text_filter:
        highlight_filter = HighlightedChartTextFilter(session_id)
        text_filters.append(highlight_filter)

    if tts_provider == TTSProvider.ELEVENLABS.value and voice_name == VoiceName.RHEA.value:
        logger.info("Using ElevenLabs TTS service for RHEA voice.")
        return ElevenLabsTTSService(
            api_key=config.ELEVENLABS_API_KEY,
            voice_id=config.ELEVENLABS_RHEA_VOICE_ID,
            model_id=config.ELEVENLABS_MODEL_ID,
            params=ElevenLabsTTSService.InputParams(speed=0.8, language=Language.EN_IN),
            text_filters=text_filters,
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
        params=GoogleTTSService.InputParams(
            language=Language.EN_IN,
        ),
        credentials=config.GOOGLE_CREDENTIALS_JSON,
        text_filters=text_filters,
    )

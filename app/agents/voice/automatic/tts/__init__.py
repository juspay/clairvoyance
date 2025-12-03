import json
from typing import Optional

from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.google.tts import GoogleTTSService
from pipecat.transcriptions.language import Language

from app.agents.voice.automatic.features.charts.highlight_filter import (
    HighlightedChartTextFilter,
)
from app.agents.voice.automatic.types import TTSProvider, VoiceName
from app.core.config.static import (
    ELEVENLABS_API_KEY,
    ELEVENLABS_MODEL_ID,
    ELEVENLABS_RHEA_VOICE_ID,
    ELEVENLABS_TTS_SPEED,
    GOOGLE_BRET_VOICE,
    GOOGLE_CREDENTIALS_JSON,
    GOOGLE_MIA_VOICE,
)
from app.core.logger import logger


def get_tts_service(
    tts_provider: str | None = None,
    voice_name: str | None = None,
    session_id: Optional[str] = None,
    enable_chart_text_filter: bool | None = None,
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

    if (
        tts_provider == TTSProvider.ELEVENLABS.value
        and voice_name == VoiceName.RHEA.value
    ):
        if not ELEVENLABS_API_KEY:
            logger.error("ElevenLabs API key is not configured")
            raise ValueError("ElevenLabs API key is required for RHEA voice")

        logger.info("Using ElevenLabs TTS service for RHEA voice.")
        return ElevenLabsTTSService(
            api_key=ELEVENLABS_API_KEY,
            voice_id=ELEVENLABS_RHEA_VOICE_ID,
            model_id=ELEVENLABS_MODEL_ID,
            params=ElevenLabsTTSService.InputParams(
                speed=ELEVENLABS_TTS_SPEED, language=Language.EN_IN
            ),
            text_filters=text_filters,
        )

    voice_id = GOOGLE_BRET_VOICE  # Default to BRET
    if tts_provider == TTSProvider.GOOGLE.value:
        if voice_name == VoiceName.MIA.value:
            voice_id = GOOGLE_MIA_VOICE
            logger.info(f"Using Google TTS service with MIA voice.")
        else:
            logger.info(f"Using Google TTS service with BRET voice.")

        # Minimal secure logging for Google credentials
        if GOOGLE_CREDENTIALS_JSON:
            try:
                if isinstance(GOOGLE_CREDENTIALS_JSON, str):
                    parsed = json.loads(GOOGLE_CREDENTIALS_JSON)
                    logger.info(
                        f"Google credentials: Valid JSON, project={parsed.get('project_id')}"
                    )
                else:
                    logger.info(
                        f"Google credentials: Dict format, project={GOOGLE_CREDENTIALS_JSON.get('project_id')}"
                    )
            except json.JSONDecodeError as e:
                logger.error(
                    f"Google credentials: JSON parsing failed at position {e.pos}"
                )
        else:
            logger.warning("Google credentials: Not provided")

    return GoogleTTSService(
        voice_id=voice_id,
        params=GoogleTTSService.InputParams(
            language=Language.EN_IN,
        ),
        credentials=GOOGLE_CREDENTIALS_JSON,
        text_filters=text_filters,
    )

from typing import Optional

from pipecat.transcriptions.language import Language

from app.ai.voice.agents.automatic.features.charts.highlight_filter import (
    HighlightedChartTextFilter,
)
from app.ai.voice.agents.automatic.types import TTSProvider, VoiceName
from app.ai.voice.tts import (
    ElevenLabsConfig,
    GoogleConfig,
    SarvamTTSConfig,
    build_elevenlabs_tts,
    build_google_tts,
    build_sarvam_tts,
)
from app.core.config.dynamic import (
    SARVAM_TTS_LANGUAGE_CODE,
    SARVAM_TTS_MODEL,
    SARVAM_TTS_PACE,
    SARVAM_TTS_PITCH,
    SARVAM_TTS_VOICE_ID,
)
from app.core.config.static import (
    ELEVENLABS_API_KEY,
    ELEVENLABS_MODEL_ID,
    ELEVENLABS_RHEA_VOICE_ID,
    ELEVENLABS_TTS_SPEED,
    GOOGLE_BRET_VOICE,
    GOOGLE_CREDENTIALS_JSON,
    GOOGLE_MIA_VOICE,
    SARVAM_API_KEY,
)
from app.core.logger import logger


async def get_tts_service(
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

    if voice_name == VoiceName.RHEA.value or tts_provider == TTSProvider.SARVAM.value:
        if not SARVAM_API_KEY:
            logger.error("SARVAM_API_KEY is not set. Sarvam TTS cannot be used.")
            raise ValueError("SARVAM_API_KEY is required for Sarvam TTS")

        sarvam_tts_language_code = await SARVAM_TTS_LANGUAGE_CODE()
        sarvam_tts_model = await SARVAM_TTS_MODEL()
        sarvam_tts_voice_id = await SARVAM_TTS_VOICE_ID()
        sarvam_tts_pitch = await SARVAM_TTS_PITCH()
        sarvam_tts_pace = await SARVAM_TTS_PACE()

        return build_sarvam_tts(
            SarvamTTSConfig(
                api_key=SARVAM_API_KEY,
                voice_id=sarvam_tts_voice_id,
                model=sarvam_tts_model,
                pitch=sarvam_tts_pitch,
                pace=sarvam_tts_pace,
                language_code=sarvam_tts_language_code,
            )
        )

    if (
        tts_provider == TTSProvider.ELEVENLABS.value
        and voice_name == VoiceName.RHEA.value
    ):
        logger.info("Using ElevenLabs TTS service for RHEA voice.")
        return build_elevenlabs_tts(
            ElevenLabsConfig(
                api_key=ELEVENLABS_API_KEY,
                voice_id=ELEVENLABS_RHEA_VOICE_ID,
                model_id=ELEVENLABS_MODEL_ID,
                speed=ELEVENLABS_TTS_SPEED,
                language=Language.EN_IN,
                text_filters=text_filters,
            )
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
                import json

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

    return build_google_tts(
        GoogleConfig(
            voice_id=voice_id,
            language=Language.EN_IN,
            credentials=GOOGLE_CREDENTIALS_JSON,
            text_filters=text_filters,
        )
    )

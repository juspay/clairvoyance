from typing import Optional

from deepgram import LiveOptions
from pipecat.services.assemblyai.stt import AssemblyAISTTService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.google.stt import GoogleSTTService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.transcriptions.language import Language

from app.agents.voice.automatic.types import VoiceName
from app.core import config
from app.core.logger import logger


def get_stt_service(voice_name: Optional[str] = None):
    """
    Returns an STT service instance based on the environment configuration.

    Args:
        voice_name: Voice name to determine STT provider override for specific voices
    """
    # Check for MIA voice with OpenAI override
    if voice_name == VoiceName.MIA.value and config.ENABLE_OPENAI_FOR_MIA:
        if not config.OPENAI_STT_API_KEY:
            raise ValueError(
                "OPENAI_STT_API_KEY is required when ENABLE_OPENAI_FOR_MIA=true and voice is MIA"
            )

        logger.info(
            f"Using OpenAI STT service for MIA voice (override enabled) with model: {config.ENFORCED_OPENAI_STT_MODEL}"
        )
        return OpenAISTTService(
            api_key=config.OPENAI_STT_API_KEY,
            model=config.ENFORCED_OPENAI_STT_MODEL,
            language=Language.EN,
            # Optimized prompt for business analytics voice agent
            prompt=config.AUTOMATIC_OPENAI_STT_PROMPT,
            temperature=0.0,  # Deterministic output for consistency
        )

    # Default behavior - use configured STT provider
    if config.STT_PROVIDER == "assemblyai":
        if not config.ASSEMBLYAI_API_KEY:
            raise ValueError(
                "ASSEMBLYAI_API_KEY is required when STT_PROVIDER=assemblyai"
            )

        logger.info("Using AssemblyAI STT service with Silero VAD-based turn detection")
        return AssemblyAISTTService(
            api_key=config.ASSEMBLYAI_API_KEY,
            # Use Silero VAD for turn detection instead of AssemblyAI's built-in turn detection
            vad_force_turn_endpoint=True,
            # No connection_params needed since we're using VAD for turn detection
        )
    elif config.STT_PROVIDER == "openai":
        if not config.OPENAI_STT_API_KEY:
            raise ValueError(
                "OPENAI_STT_API_KEY or OPENAI_API_KEY is required when STT_PROVIDER=openai"
            )

        logger.info(
            f"Using OpenAI STT service ({config.OPENAI_STT_MODEL}) with Silero VAD-based turn detection"
        )
        return OpenAISTTService(
            api_key=config.OPENAI_STT_API_KEY,
            model=config.OPENAI_STT_MODEL,
            language=Language.EN,
            # Optimized prompt for business analytics voice agent
            prompt=config.AUTOMATIC_OPENAI_STT_PROMPT,
            temperature=0.0,  # Deterministic output for consistency
        )
    elif config.STT_PROVIDER == "deepgram":
        if not config.DEEPGRAM_API_KEY:
            raise ValueError("DEEPGRAM_API_KEY is required when STT_PROVIDER=deepgram")

        # Determine language configuration based on settings
        if config.DEEPGRAM_AUTO_DETECT_LANGUAGE:
            language_config = "multi"  # Automatic detection
        else:
            language_config = (
                config.DEEPGRAM_LANGUAGE
            )  # Single language (current behavior)

        # Configure Deepgram with smart turn detection and audio enhancement
        live_options = LiveOptions(
            model=config.DEEPGRAM_MODEL,
            language=language_config,
            smart_format=config.DEEPGRAM_SMART_FORMAT,
            punctuate=config.DEEPGRAM_PUNCTUATE,
            endpointing=config.DEEPGRAM_ENDPOINTING,  # Smart turn detection
            vad_events=config.DEEPGRAM_VAD_EVENTS,  # Built-in VAD
            utterance_end_ms=config.DEEPGRAM_UTTERANCE_END_MS,
            no_delay=config.DEEPGRAM_NO_DELAY,  # Real-time processing
            interim_results=True,
            profanity_filter=config.DEEPGRAM_PROFANITY_FILTER,
            # Enhanced for Indian English and business terms
            numerals=config.DEEPGRAM_NUMERALS,  # Better number recognition
            diarize=config.DEEPGRAM_DIARIZE,  # Speaker identification
        )

        logger.info(
            f"Using Deepgram STT service with model: {config.DEEPGRAM_MODEL}, "
            f"language: {language_config} "
            f"(VAD: {config.DEEPGRAM_VAD_EVENTS}, Endpointing: {config.DEEPGRAM_ENDPOINTING})"
        )
        return DeepgramSTTService(
            api_key=config.DEEPGRAM_API_KEY, live_options=live_options
        )
    else:  # Default to Google STT
        logger.info("Using Google STT service with VAD-based turn detection")
        return GoogleSTTService(
            params=GoogleSTTService.InputParams(
                languages=[Language.EN_US, Language.EN_IN], enable_interim_results=False
            ),
            credentials=config.GOOGLE_CREDENTIALS_JSON,
        )

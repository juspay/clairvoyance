from app.core.logger import logger
from app.core import config

from pipecat.services.google.stt import GoogleSTTService
from pipecat.services.assemblyai.stt import AssemblyAISTTService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.transcriptions.language import Language

def get_stt_service():
    """
    Returns an STT service instance based on the environment configuration.
    """
    if config.STT_PROVIDER == "assemblyai":
        if not config.ASSEMBLYAI_API_KEY:
            raise ValueError("ASSEMBLYAI_API_KEY is required when STT_PROVIDER=assemblyai")
        
        logger.info("Using AssemblyAI STT service with Silero VAD-based turn detection")
        return AssemblyAISTTService(
            api_key=config.ASSEMBLYAI_API_KEY,
            # Use Silero VAD for turn detection instead of AssemblyAI's built-in turn detection
            vad_force_turn_endpoint=True,
            # No connection_params needed since we're using VAD for turn detection
        )
    elif config.STT_PROVIDER == "openai":
        if not config.OPENAI_STT_API_KEY:
            raise ValueError("OPENAI_STT_API_KEY or OPENAI_API_KEY is required when STT_PROVIDER=openai")
        
        logger.info(f"Using OpenAI STT service ({config.OPENAI_STT_MODEL}) with Silero VAD-based turn detection")
        return OpenAISTTService(
            api_key=config.OPENAI_STT_API_KEY,
            model=config.OPENAI_STT_MODEL,
            language=Language.EN,
            # Optimized prompt for business analytics voice agent
            prompt="Transcribe business and financial terms accurately. Include proper names, numbers, and technical terms exactly as spoken.",
            temperature=0.0,  # Deterministic output for consistency
        )
    else:  # Default to Google STT
        logger.info("Using Google STT service with VAD-based turn detection")
        return GoogleSTTService(
            params=GoogleSTTService.InputParams(languages=[Language.EN_US, Language.EN_IN], enable_interim_results=False),
            credentials=config.GOOGLE_CREDENTIALS_JSON
        )
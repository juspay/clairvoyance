from app.core.logger import logger
from app.core import config

from pipecat.services.google.stt import GoogleSTTService
from pipecat.services.assemblyai.stt import AssemblyAISTTService, AssemblyAIConnectionParams
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.speechmatics.stt import SpeechmaticsSTTService, OperatingPoint, EndOfUtteranceMode, AdditionalVocabEntry
from pipecat.transcriptions.language import Language
from app.agents.voice.automatic.types import VoiceName
from typing import Optional

def get_stt_provider_name(voice_name: Optional[str] = None) -> str:
    """
    Returns the name of the STT provider being used for display purposes.
    
    Args:
        voice_name: Voice name to determine STT provider override for specific voices
        
    Returns:
        Human-readable STT provider name
    """
    # Check for MIA voice with OpenAI override
    if voice_name == VoiceName.MIA.value and config.ENABLE_OPENAI_FOR_MIA:
        return "OpenAI Whisper (MIA Override)"
    
    # Default behavior - use configured STT provider
    if config.STT_PROVIDER == "assemblyai":
        return "AssemblyAI STT"
    elif config.STT_PROVIDER == "openai":
        return f"OpenAI {config.OPENAI_STT_MODEL}"
    elif config.STT_PROVIDER == "speechmatics":
        return "Speechmatics STT"
    else:  # Default to Google STT
        return "Google Cloud STT"

def get_stt_service(voice_name: Optional[str] = None):
    """
    Returns an optimized STT service instance based on environment configuration.
    
    All services are configured with production-ready settings for:
    - Low latency real-time transcription
    - Indian business terminology recognition
    - Enhanced accuracy for financial and e-commerce terms
    - Optimal performance tuning based on Pipecat best practices
    
    Args:
        voice_name: Voice name to determine STT provider override for specific voices
    """
    logger.info(f"Initializing STT service: provider={config.STT_PROVIDER}, voice={voice_name}")
    logger.info(f"Performance settings: timeout={config.STT_CONNECTION_TIMEOUT}s, fallback={config.STT_ENABLE_FALLBACK}")
    # Check for MIA voice with OpenAI override
    if voice_name == VoiceName.MIA.value and config.ENABLE_OPENAI_FOR_MIA:
        if not config.OPENAI_STT_API_KEY:
            raise ValueError("OPENAI_STT_API_KEY is required when ENABLE_OPENAI_FOR_MIA=true and voice is MIA")
        
        logger.info("Using OpenAI STT service for MIA voice (override enabled)")
        return OpenAISTTService(
            api_key=config.OPENAI_STT_API_KEY,
            model=config.OPENAI_STT_MODEL,
            language=Language.EN,
            # Optimized prompt for business analytics voice agent
            prompt="Always strive to provide an accurate transcription in English, preserving the original meaning and context. Represent Indian number formats (lakhs, crores, etc.) in their natural spoken form. Capture business and financial terminology with precision. Ensure all proper names, numbers, and technical terms are recorded exactly as spoken, without any alterations.",
            temperature=0.0,  # Deterministic output for consistency
        )
    
    # Default behavior - use configured STT provider
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
            raise ValueError("OPENAI_STT_API_KEY is required when STT_PROVIDER=openai")

        logger.info(f"Using OpenAI STT service ({config.OPENAI_STT_MODEL}) with production optimization")
        return OpenAISTTService(
            api_key=config.OPENAI_STT_API_KEY,
            model=config.OPENAI_STT_MODEL,
            language=Language.EN,
            # Optimized prompt for business analytics voice agent
            prompt="Transcribe Indian languages accurately. Recognize and render Indian numbers (lakhs, crores, etc.) in their natural spoken form. Ensure business and financial terms are captured with precision. Always include proper names, numbers, and technical terms exactly as spoken, without modification.",
            temperature=0.0,  # Deterministic output for consistency
        )
    elif config.STT_PROVIDER == "speechmatics":
        if not config.SPEECHMATICS_API_KEY:
            raise ValueError("SPEECHMATICS_API_KEY is required when STT_PROVIDER=speechmatics")

        logger.info("Using Speechmatics STT service with enhanced real-time configuration")
        
        # Comprehensive business vocabulary for Indian e-commerce and analytics
        business_vocab = [
            # Platform specific
            AdditionalVocabEntry(content="Clairvoyance", sounds_like=["claire voyance", "clear voyance", "clevoyance"]),
            AdditionalVocabEntry(content="Breeze", sounds_like=["breez", "breezy"]),
            AdditionalVocabEntry(content="Juspay", sounds_like=["just pay", "jus pay"]),
            
            # Analytics and metrics
            AdditionalVocabEntry(content="analytics", sounds_like=["an a lytics", "analytix"]),
            AdditionalVocabEntry(content="dashboard", sounds_like=["dash board", "dash bord"]),
            AdditionalVocabEntry(content="metrics", sounds_like=["met rics", "metrix"]),
            AdditionalVocabEntry(content="KPIs", sounds_like=["K P I s", "key pis"]),
            AdditionalVocabEntry(content="funnel", sounds_like=["fun nel", "funel"]),
            AdditionalVocabEntry(content="cohort", sounds_like=["co hort", "ko hort"]),
            
            # Financial terms
            AdditionalVocabEntry(content="revenue", sounds_like=["rev enue", "revenu"]),
            AdditionalVocabEntry(content="conversion", sounds_like=["con version", "convertion"]),
            AdditionalVocabEntry(content="ARPU", sounds_like=["A R P U", "arpu"]),
            AdditionalVocabEntry(content="LTV", sounds_like=["L T V", "lifetime value"]),
            AdditionalVocabEntry(content="ROAS", sounds_like=["R O A S", "return on ad spend"]),
            
            # Indian currency
            AdditionalVocabEntry(content="lakhs", sounds_like=["lacs", "lack", "lakh"]),
            AdditionalVocabEntry(content="crores", sounds_like=["crore", "core", "kror"]),
            AdditionalVocabEntry(content="rupees", sounds_like=["rupee", "rs", "inr"]),
            AdditionalVocabEntry(content="paisa", sounds_like=["paise", "paesa"]),
            
            # E-commerce terms
            AdditionalVocabEntry(content="checkout", sounds_like=["check out", "chekout"]),
            AdditionalVocabEntry(content="cart", sounds_like=["kart", "shopping cart"]),
            AdditionalVocabEntry(content="SKU", sounds_like=["S K U", "sku"]),
            AdditionalVocabEntry(content="GMV", sounds_like=["G M V", "gross merchandise"]),
            AdditionalVocabEntry(content="AOV", sounds_like=["A O V", "average order"]),
            
            # Payment terms
            AdditionalVocabEntry(content="UPI", sounds_like=["U P I", "upi"]),
            AdditionalVocabEntry(content="NEFT", sounds_like=["N E F T", "neft"]),
            AdditionalVocabEntry(content="RTGS", sounds_like=["R T G S", "rtgs"]),
            AdditionalVocabEntry(content="netbanking", sounds_like=["net banking", "net bank"]),
        ]
        
        return SpeechmaticsSTTService(
            api_key=config.SPEECHMATICS_API_KEY,
            params=SpeechmaticsSTTService.InputParams(
                # Language configuration
                language=Language.EN,
                domain="finance",  # Financial domain optimization
                
                # Optimized for accuracy vs latency
                operating_point=OperatingPoint.ENHANCED,  # Best accuracy
                
                # Real-time configuration with performance tuning
                enable_partials=True,  # Enable partial transcriptions
                max_delay=0.7,  # Faster processing (further optimized)
                end_of_utterance_silence_trigger=0.4,  # Quicker turn detection
                end_of_utterance_mode=EndOfUtteranceMode.ADAPTIVE,  # Smart turn detection
                
                # Voice Activity Detection
                enable_vad=True,  # Better end-of-utterance detection
                
                # Enhanced vocabulary for business terms
                additional_vocab=business_vocab,
                
                # Advanced speaker diarization
                enable_diarization=True,
                speaker_sensitivity=0.6,  # Good balance for similar voices
                max_speakers=4,  # Reasonable limit for meetings
                prefer_current_speaker=True,  # Better speaker continuity
                
                # Punctuation and formatting enhancements
                
                
                # Audio processing optimization
                chunk_size=160,  # Optimized chunk size for real-time
                audio_encoding="pcm_s16le",  # Standard PCM encoding
                
                # Enhanced accuracy features
                enable_entities=True,  # Entity recognition for better context
                enable_translation=False,  # Disable translation for performance
            )
        )
    else:  # Default to Google STT
        logger.info("Using Google STT service with enhanced production settings")
        return GoogleSTTService(
            params=GoogleSTTService.InputParams(languages=[Language.EN_US, Language.EN_IN], enable_interim_results=False, model="latest_long"),
            credentials=config.GOOGLE_CREDENTIALS_JSON
        )
"""
Speechmatics STT Service with Audio Filtering Support

Provides speech-to-text functionality using Speechmatics API with built-in
audio filtering to remove background speech and noise.
"""

from typing import List, Optional
from loguru import logger

from pipecat.services.speechmatics.stt import (
    SpeechmaticsSTTService, 
    AdditionalVocabEntry,
    EndOfUtteranceMode,
    OperatingPoint
)
from pipecat.transcriptions.language import Language
from app.core import config


def create_speechmatics_stt_service(
    languages: List[Language] = None,
    custom_vocabulary: Optional[List[str]] = None
) -> SpeechmaticsSTTService:
    """
    Create and configure Speechmatics STT service with audio filtering.
    
    Args:
        languages: List of languages to support (defaults to EN_US and EN_IN)
        
    Returns:
        Configured SpeechmaticsSTTService instance
        
    Raises:
        ValueError: If SPEECHMATICS_API_KEY is not configured
        Exception: If service creation fails
    """
    if not config.SPEECHMATICS_API_KEY:
        raise ValueError("SPEECHMATICS_API_KEY is required for Speechmatics STT")
    
    if languages is None:
        languages = [Language.EN_US, Language.EN_IN]
    
    logger.info(f"[SPEECHMATICS] Creating STT service with languages: {[lang.value for lang in languages]}")
    logger.info(f"[SPEECHMATICS] Audio filtering volume threshold: {config.SPEECHMATICS_VOLUME_THRESHOLD}")
    logger.info(f"[SPEECHMATICS] Operating point: {config.SPEECHMATICS_OPERATING_POINT}")
    logger.info(f"[SPEECHMATICS] Volume labeling: {config.SPEECHMATICS_ENABLE_VOLUME_LABELING}")
    logger.info(f"[SPEECHMATICS] Diarization enabled: {config.SPEECHMATICS_ENABLE_DIARIZATION}")
    logger.info(f"[SPEECHMATICS] End-of-utterance mode: {config.SPEECHMATICS_END_OF_UTTERANCE_MODE}")
    logger.info(f"[SPEECHMATICS] Max delay: {config.SPEECHMATICS_MAX_DELAY}s")
    
    try:
        # Prepare custom vocabulary if provided
        additional_vocab = []
        if custom_vocabulary:
            additional_vocab = [
                AdditionalVocabEntry(content=word, sounds_like=[word.lower()])
                for word in custom_vocabulary
            ]
        
        # Add default business/e-commerce vocabulary
        default_vocab = [
            ("Clairvoyance", ["claire voyance", "clear voyance"]),
            ("analytics", ["an a lytics"]),
            ("dashboard", ["dash board"]), 
            ("metrics", ["met rics"]),
            ("revenue", ["rev enue"]),
            ("conversion", ["con version"]),
            ("customer", ["cust omer"]),
            ("order", ["or der"]),
            ("payment", ["pay ment"]),
            ("checkout", ["check out"]),
            ("inventory", ["in ventory"]),
            ("product", ["prod uct"]),
            ("catalog", ["cat alog"]),
            ("campaign", ["cam paign"]),
            ("marketing", ["mark eting"])
        ]
        additional_vocab.extend([
            AdditionalVocabEntry(content=word, sounds_like=sounds)
            for word, sounds in default_vocab
        ])
        
        # Determine end of utterance mode
        eou_mode = EndOfUtteranceMode.ADAPTIVE if config.SPEECHMATICS_END_OF_UTTERANCE_MODE.lower() == "adaptive" else EndOfUtteranceMode.FIXED
        
        # Determine operating point
        op_point = OperatingPoint.ENHANCED if config.SPEECHMATICS_OPERATING_POINT.lower() == "enhanced" else OperatingPoint.STANDARD
        
        # Create Speechmatics STT service with enhanced configuration
        stt_service = SpeechmaticsSTTService(
            api_key=config.SPEECHMATICS_API_KEY,
            params=SpeechmaticsSTTService.InputParams(
                # Language configuration - use base language EN instead of EN_US
                language=Language.EN,
                output_locale=languages[0] if languages else Language.EN_US,
                domain=config.SPEECHMATICS_DOMAIN if config.SPEECHMATICS_DOMAIN else None,
                
                # Operating point for accuracy vs latency tradeoff
                operating_point=op_point,
                
                # Audio filtering configuration for background speech removal
                audio_filtering_config={
                    "volume_threshold": config.SPEECHMATICS_VOLUME_THRESHOLD
                } if config.SPEECHMATICS_VOLUME_THRESHOLD > 0 else {},
                
                # Enable volume labeling for each word if configured
                enable_volume_labeling=config.SPEECHMATICS_ENABLE_VOLUME_LABELING,
                
                # Voice Activity Detection for better end-of-utterance detection
                enable_vad=True,
                
                # Enable partial transcriptions for real-time feedback
                enable_partials=config.SPEECHMATICS_ENABLE_PARTIALS,
                
                # Optimized settings for better real-time performance
                max_delay=config.SPEECHMATICS_MAX_DELAY,
                end_of_utterance_silence_trigger=config.SPEECHMATICS_END_OF_UTTERANCE_SILENCE,
                end_of_utterance_mode=eou_mode,
                
                # Speaker diarization for better accuracy
                enable_diarization=config.SPEECHMATICS_ENABLE_DIARIZATION,
                speaker_sensitivity=config.SPEECHMATICS_SPEAKER_SENSITIVITY,
                max_speakers=config.SPEECHMATICS_MAX_SPEAKERS if config.SPEECHMATICS_MAX_SPEAKERS > 0 else None,
                ignore_speakers=["__ASSISTANT__"],  # Filter out assistant speech
                
                # Custom vocabulary for better domain-specific recognition
                
                # Audio encoding settings
                chunk_size=config.SPEECHMATICS_CHUNK_SIZE,
            )
        )
        
        logger.info("[SPEECHMATICS] ✅ STT service created successfully")
        return stt_service
        
    except Exception as e:
        logger.error(f"[SPEECHMATICS] ❌ Failed to create STT service: {e}")
        raise


def validate_speechmatics_config() -> bool:
    """
    Validate Speechmatics configuration.
    
    Returns:
        True if configuration is valid, False otherwise
    """
    if not config.USE_SPEECHMATICS:
        return False
        
    if not config.SPEECHMATICS_API_KEY:
        logger.warning("[SPEECHMATICS] ⚠️ API key not configured")
        return False
        
    if config.SPEECHMATICS_VOLUME_THRESHOLD < 0 or config.SPEECHMATICS_VOLUME_THRESHOLD > 100:
        logger.warning(f"[SPEECHMATICS] ⚠️ Invalid volume threshold: {config.SPEECHMATICS_VOLUME_THRESHOLD}")
        return False
        
    if config.SPEECHMATICS_OPERATING_POINT not in ["enhanced", "standard"]:
        logger.warning(f"[SPEECHMATICS] ⚠️ Invalid operating point: {config.SPEECHMATICS_OPERATING_POINT}")
        return False
        
    if config.SPEECHMATICS_SPEAKER_SENSITIVITY < 0 or config.SPEECHMATICS_SPEAKER_SENSITIVITY > 1:
        logger.warning(f"[SPEECHMATICS] ⚠️ Invalid speaker sensitivity: {config.SPEECHMATICS_SPEAKER_SENSITIVITY}")
        return False
        
    if config.SPEECHMATICS_END_OF_UTTERANCE_MODE not in ["adaptive", "fixed"]:
        logger.warning(f"[SPEECHMATICS] ⚠️ Invalid end-of-utterance mode: {config.SPEECHMATICS_END_OF_UTTERANCE_MODE}")
        return False
        
    if config.SPEECHMATICS_END_OF_UTTERANCE_SILENCE <= 0 or config.SPEECHMATICS_END_OF_UTTERANCE_SILENCE > 5:
        logger.warning(f"[SPEECHMATICS] ⚠️ Invalid end-of-utterance silence: {config.SPEECHMATICS_END_OF_UTTERANCE_SILENCE}")
        return False
        
    if config.SPEECHMATICS_MAX_DELAY <= 0 or config.SPEECHMATICS_MAX_DELAY > 10:
        logger.warning(f"[SPEECHMATICS] ⚠️ Invalid max delay: {config.SPEECHMATICS_MAX_DELAY}")
        return False
        
    return True


def log_speechmatics_config():
    """Log current Speechmatics configuration for debugging."""
    logger.info("[SPEECHMATICS] Configuration:")
    logger.info(f"  - USE_SPEECHMATICS: {config.USE_SPEECHMATICS}")
    logger.info(f"  - API_KEY configured: {bool(config.SPEECHMATICS_API_KEY)}")
    logger.info(f"  - Base URL: {config.SPEECHMATICS_RT_URL}")
    logger.info(f"  - Volume threshold: {config.SPEECHMATICS_VOLUME_THRESHOLD}")
    logger.info(f"  - Operating point: {config.SPEECHMATICS_OPERATING_POINT}")
    logger.info(f"  - Volume labeling: {config.SPEECHMATICS_ENABLE_VOLUME_LABELING}")
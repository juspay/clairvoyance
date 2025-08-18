"""
Speaker Diarization Service using Speechmatics STT with voice locking capabilities.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Dict, Any, Protocol

from pydantic import BaseModel, Field, validator
from pipecat.services.speechmatics.stt import SpeechmaticsSTTService
from pipecat.services.google.stt import GoogleSTTService
from pipecat.transcriptions.language import Language
from pipecat.frames.frames import TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.core.logger import logger
from app.core.config import (
    SPEECHMATICS_API_KEY,
    ENABLE_SPEAKER_DIARIZATION,
    SPEAKER_SENSITIVITY,
    MAX_SPEAKERS,
    ENABLE_VOICE_LOCKING,
    GOOGLE_CREDENTIALS_JSON
)


# Configuration Models
class SpeakerDiarizationConfig(BaseModel):
    """Configuration for speaker diarization with validation."""
    enable_diarization: bool = True
    enable_voice_locking: bool = True
    speaker_sensitivity: float = Field(default=0.5, ge=0.0, le=1.0)
    max_speakers: int = Field(default=5, ge=1, le=10)
    detection_window: float = Field(default=5.0, ge=1.0, le=30.0)
    min_speech_duration: float = Field(default=2.0, ge=0.5, le=10.0)
    session_ttl_seconds: int = Field(default=1800, ge=300, le=7200)  # 30 min default
    
    @validator('speaker_sensitivity')
    def validate_sensitivity(cls, v):
        if not 0.0 <= v <= 1.0:
            raise ValueError('speaker_sensitivity must be between 0.0 and 1.0')
        return v


# Custom Exceptions
class STTServiceError(Exception):
    """Base exception for STT service errors."""
    pass


class STTConnectionError(STTServiceError):
    """STT service connection/network errors."""
    pass


class STTAuthenticationError(STTServiceError):
    """STT service authentication errors."""
    pass


class STTRateLimitError(STTServiceError):
    """STT service rate limit errors."""
    pass


# Health Status
class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class HealthCheck:
    """Health check result for STT services."""
    status: HealthStatus
    last_check: float
    error_count: int = 0
    last_error: Optional[str] = None
    response_time_ms: Optional[float] = None


# STT Provider Protocol
class STTProvider(Protocol):
    """Protocol defining the interface for STT providers."""
    
    async def transcribe(self, audio_data: bytes) -> Optional[TranscriptionFrame]:
        """Transcribe audio data and return transcription frame."""
        ...
    
    async def health_check(self) -> HealthCheck:
        """Check the health of the STT service."""
        ...
    
    def supports_speaker_diarization(self) -> bool:
        """Check if provider supports speaker diarization."""
        ...


# Circuit Breaker States
class CircuitState(str, Enum):
    CLOSED = "closed"  # Normal operation
    OPEN = "open"      # Failing, using fallback
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class SpeakerInfo:
    """Information about a detected speaker."""
    speaker_id: str
    first_detected_at: float
    total_speech_time: float
    last_active_at: float
    is_locked: bool = False
    
    def is_expired(self, ttl_seconds: int) -> bool:
        """Check if speaker info has expired based on TTL."""
        return time.time() - self.last_active_at > ttl_seconds
    
    def update_activity(self, speech_duration: float = 0.0):
        """Update speaker activity timestamp and speech time."""
        self.last_active_at = time.time()
        if speech_duration > 0:
            self.total_speech_time += speech_duration


class VoiceLockingProcessor(FrameProcessor):
    """
    Processor that implements voice locking functionality with speaker diarization.
    Monitors transcription frames with speaker IDs and locks onto the first active speaker.
    Includes memory management and performance optimizations.
    """
    
    def __init__(self, config: SpeakerDiarizationConfig):
        """
        Initialize the voice locking processor.
        
        Args:
            config: Configuration for speaker diarization and voice locking
        """
        super().__init__()
        self.config = config
        
        # Speaker tracking
        self.speakers: Dict[str, SpeakerInfo] = {}
        self.locked_speaker: Optional[str] = None
        self.session_start_time: Optional[float] = None
        self.first_speaker_detected: Optional[str] = None
        
        # Performance optimization
        self._last_cleanup_time = time.time()
        self._cleanup_interval = 60.0  # Cleanup every minute
        
        # Constants for speech estimation
        self.WORDS_PER_SECOND = 2.5  # More accurate than 0.6s per word
        
        logger.info(f"VoiceLockingProcessor initialized - voice_locking: {config.enable_voice_locking}")
    
    async def process_frame(self, frame, direction: FrameDirection):
        """Process frames and implement voice locking logic."""
        await super().process_frame(frame, direction)
        
        # Handle transcription frames for voice locking
        if isinstance(frame, TranscriptionFrame):
            await self._handle_transcription_frame(frame)
        else:
            # Pass through all other frames
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)
        
        # Periodic cleanup of expired speakers
        await self._cleanup_expired_speakers()
    
    async def _handle_transcription_frame(self, frame: TranscriptionFrame):
        """Handle transcription frames with speaker filtering."""
        current_time = time.time()
        
        # Initialize session if needed
        if self.session_start_time is None:
            self.session_start_time = current_time
            logger.debug("Voice locking session started")
        
        # Extract speaker info - handle different STT provider formats
        speaker_id = self._extract_speaker_id(frame)
        transcription_text = getattr(frame, 'text', '')
        
        # Skip empty transcriptions
        if not transcription_text.strip():
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)
            return
        
        # Process speaker information if voice locking enabled
        if speaker_id and self.config.enable_voice_locking:
            await self._process_speaker_info(speaker_id, transcription_text, current_time)
        
        # Apply voice locking filter
        if self._should_pass_frame(speaker_id):
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)
        else:
            logger.debug(f"Filtered frame from speaker {speaker_id} (locked on {self.locked_speaker})")
    
    def _extract_speaker_id(self, frame: TranscriptionFrame) -> Optional[str]:
        """Extract speaker ID from transcription frame, handling different STT providers."""
        # Speechmatics uses 'user_id' for speaker identification
        speaker_id = getattr(frame, 'user_id', None)
        if speaker_id:
            return speaker_id
        
        # Check for other possible speaker ID attributes
        for attr in ['speaker_id', 'speaker', 'user']:
            if hasattr(frame, attr):
                return getattr(frame, attr)
        
        return None
    
    async def _process_speaker_info(self, speaker_id: str, transcription_text: str, current_time: float):
        """Process and track speaker information."""
        # Calculate speech duration
        word_count = len(transcription_text.split())
        estimated_duration = word_count / self.WORDS_PER_SECOND
        
        # Track or update speaker
        if speaker_id not in self.speakers:
            self.speakers[speaker_id] = SpeakerInfo(
                speaker_id=speaker_id,
                first_detected_at=current_time,
                total_speech_time=estimated_duration,
                last_active_at=current_time
            )
            logger.debug(f"New speaker detected: {speaker_id}")
            
            # Lock onto the first speaker detected
            if self.first_speaker_detected is None:
                self.first_speaker_detected = speaker_id
                await self._lock_onto_speaker(speaker_id)
        else:
            # Update existing speaker activity
            self.speakers[speaker_id].update_activity(estimated_duration)
    
    def _should_pass_frame(self, speaker_id: Optional[str]) -> bool:
        """Determine if frame should pass through voice locking filter."""
        if not self.config.enable_voice_locking:
            return True
        
        if self.locked_speaker is None:
            return True
        
        # Pass frames from locked speaker or frames without speaker info
        return speaker_id == self.locked_speaker or speaker_id is None
    
    async def _lock_onto_speaker(self, speaker_id: str):
        """Lock onto a specific speaker."""
        self.locked_speaker = speaker_id
        if speaker_id in self.speakers:
            self.speakers[speaker_id].is_locked = True
        
        logger.info(f"Voice locked onto speaker: {speaker_id}")
        logger.debug(f"Current speakers: {list(self.speakers.keys())}")
    
    async def _cleanup_expired_speakers(self):
        """Periodically clean up expired speaker information to prevent memory leaks."""
        current_time = time.time()
        
        # Only cleanup periodically to avoid overhead
        if current_time - self._last_cleanup_time < self._cleanup_interval:
            return
        
        expired_speakers = [
            speaker_id for speaker_id, info in self.speakers.items()
            if info.is_expired(self.config.session_ttl_seconds) and not info.is_locked
        ]
        
        for speaker_id in expired_speakers:
            del self.speakers[speaker_id]
        
        if expired_speakers:
            logger.debug(f"Cleaned up {len(expired_speakers)} expired speakers")
        
        self._last_cleanup_time = current_time
    
    def get_speaker_stats(self) -> Dict[str, Any]:
        """Get statistics about detected speakers."""
        current_time = time.time()
        return {
            "locked_speaker": self.locked_speaker,
            "first_speaker": self.first_speaker_detected,
            "total_speakers": len(self.speakers),
            "session_duration": current_time - self.session_start_time if self.session_start_time else 0,
            "config": {
                "enable_voice_locking": self.config.enable_voice_locking,
                "detection_window": self.config.detection_window,
                "min_speech_duration": self.config.min_speech_duration
            },
            "speakers": {
                sid: {
                    "first_detected_at": info.first_detected_at,
                    "total_speech_time": round(info.total_speech_time, 2),
                    "last_active_at": info.last_active_at,
                    "is_locked": info.is_locked,
                    "is_expired": info.is_expired(self.config.session_ttl_seconds)
                }
                for sid, info in self.speakers.items()
            }
        }
    
    def reset_session(self):
        """Reset the voice locking session."""
        self.speakers.clear()
        self.locked_speaker = None
        self.session_start_time = None
        self.first_speaker_detected = None
        logger.info("Voice locking session reset")


class SpeakerDiarizationService:
    """
    Service for creating Speechmatics STT with speaker diarization and voice locking.
    Includes improved error handling and configuration validation.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the speaker diarization service.
        
        Args:
            api_key: Speechmatics API key. If None, uses config value.
        """
        self.api_key = api_key or SPEECHMATICS_API_KEY
        self.google_credentials = GOOGLE_CREDENTIALS_JSON
        
        # Create default configuration
        self.default_config = SpeakerDiarizationConfig(
            enable_diarization=ENABLE_SPEAKER_DIARIZATION,
            enable_voice_locking=ENABLE_VOICE_LOCKING,
            speaker_sensitivity=SPEAKER_SENSITIVITY,
            max_speakers=MAX_SPEAKERS
        )
        
        logger.debug(f"SpeakerDiarizationService initialized - API key present: {bool(self.api_key)}")
    
    def _validate_speechmatics_config(self) -> None:
        """Validate Speechmatics configuration before creating service."""
        if not self.api_key:
            raise STTAuthenticationError("SPEECHMATICS_API_KEY is required for speaker diarization")
        
        if not self.api_key.startswith(('sm_', 'SM_')):
            logger.warning("Speechmatics API key format appears invalid - should start with 'sm_'")
    
    def _validate_google_config(self) -> None:
        """Validate Google STT configuration."""
        if not self.google_credentials:
            raise STTAuthenticationError("GOOGLE_CREDENTIALS_JSON is required for Google STT fallback")
    
    def create_stt_service(self, 
                          config: Optional[SpeakerDiarizationConfig] = None,
                          languages: Optional[List[Language]] = None) -> SpeechmaticsSTTService:
        """
        Create a Speechmatics STT service with speaker diarization.
        
        Args:
            config: Speaker diarization configuration. Uses default if None.
            languages: List of languages to recognize
        
        Returns:
            Configured SpeechmaticsSTTService
            
        Raises:
            STTAuthenticationError: If API key is invalid
            STTServiceError: If service creation fails
        """
        # Use provided config or default
        config = config or self.default_config
        
        # Default languages
        if languages is None:
            languages = [Language.EN_US, Language.EN_IN]
        
        # Validate configuration
        self._validate_speechmatics_config()
        
        logger.debug("Creating Speechmatics STT service")
        logger.debug(f"Diarization enabled: {config.enable_diarization}")
        logger.debug(f"Speaker sensitivity: {config.speaker_sensitivity}")
        logger.debug(f"Max speakers: {config.max_speakers}")
        
        # Configure speaker formats for better identification
        speaker_active_format = "{text}" if config.enable_diarization else None
        
        try:
            params = SpeechmaticsSTTService.InputParams(
                languages=languages,
                enable_diarization=config.enable_diarization,
                speaker_sensitivity=config.speaker_sensitivity,
                max_speakers=config.max_speakers,
                enable_interim_results=True,
                speaker_active_format=speaker_active_format,
                # Use focus_speakers for voice locking after first speaker is detected
                focus_speakers=[] if config.enable_diarization else None,
            )
            
            stt_service = SpeechmaticsSTTService(
                api_key=self.api_key,
                params=params
            )
            
            logger.info("Speechmatics STT service created successfully")
            return stt_service
            
        except Exception as e:
            error_msg = f"Failed to create Speechmatics STT service: {e}"
            logger.error(error_msg)
            
            # Classify error types for better handling
            if "authentication" in str(e).lower() or "unauthorized" in str(e).lower():
                raise STTAuthenticationError(f"Speechmatics authentication failed: {e}")
            elif "rate limit" in str(e).lower() or "quota" in str(e).lower():
                raise STTRateLimitError(f"Speechmatics rate limit exceeded: {e}")
            elif "network" in str(e).lower() or "connection" in str(e).lower():
                raise STTConnectionError(f"Speechmatics connection failed: {e}")
            else:
                raise STTServiceError(error_msg)
    
    def create_google_stt_fallback(self, languages: Optional[List[Language]] = None) -> GoogleSTTService:
        """
        Create a Google STT service as fallback.
        
        Args:
            languages: List of languages to recognize
            
        Returns:
            Configured GoogleSTTService
            
        Raises:
            STTAuthenticationError: If credentials are invalid
        """
        if languages is None:
            languages = [Language.EN_US, Language.EN_IN]
        
        self._validate_google_config()
        
        try:
            stt_service = GoogleSTTService(
                params=GoogleSTTService.InputParams(
                    languages=languages, 
                    enable_interim_results=False
                ),
                credentials=self.google_credentials
            )
            
            logger.info("Google STT fallback service created successfully")
            return stt_service
            
        except Exception as e:
            error_msg = f"Failed to create Google STT service: {e}"
            logger.error(error_msg)
            raise STTServiceError(error_msg)
    
    def create_voice_locking_processor(self, config: Optional[SpeakerDiarizationConfig] = None) -> VoiceLockingProcessor:
        """
        Create a voice locking processor.
        
        Args:
            config: Speaker diarization configuration. Uses default if None.
            
        Returns:
            Configured VoiceLockingProcessor
        """
        config = config or self.default_config
        return VoiceLockingProcessor(config)
    
    def create_stt_with_voice_locking(self, 
                                    config: Optional[SpeakerDiarizationConfig] = None,
                                    languages: Optional[List[Language]] = None) -> tuple[SpeechmaticsSTTService, VoiceLockingProcessor]:
        """
        Create STT service and voice locking processor as a pair.
        
        Args:
            config: Speaker diarization configuration. Uses default if None.
            languages: List of languages to recognize
        
        Returns:
            Tuple of (stt_service, voice_locking_processor)
            
        Raises:
            STTServiceError: If STT service creation fails
        """
        config = config or self.default_config
        
        stt_service = self.create_stt_service(config, languages)
        voice_processor = self.create_voice_locking_processor(config)
        
        return stt_service, voice_processor
    
    def create_stt_with_fallback(self,
                               config: Optional[SpeakerDiarizationConfig] = None,
                               languages: Optional[List[Language]] = None) -> tuple[object, Optional[VoiceLockingProcessor]]:
        """
        Create STT service with automatic fallback to Google STT if Speechmatics fails.
        
        Args:
            config: Speaker diarization configuration. Uses default if None.
            languages: List of languages to recognize
            
        Returns:
            Tuple of (stt_service, voice_locking_processor or None)
            
        Note:
            voice_locking_processor will be None if falling back to Google STT
        """
        config = config or self.default_config
        
        try:
            # Try Speechmatics first
            stt_service, voice_processor = self.create_stt_with_voice_locking(config, languages)
            logger.info("Using Speechmatics STT with voice locking")
            return stt_service, voice_processor
            
        except (STTAuthenticationError, STTConnectionError, STTServiceError) as e:
            logger.warning(f"Speechmatics STT failed, falling back to Google STT: {e}")
            
            # Fallback to Google STT
            google_stt = self.create_google_stt_fallback(languages)
            logger.info("Using Google STT (no speaker diarization)")
            return google_stt, None


# Global instance
speaker_diarization_service = SpeakerDiarizationService()
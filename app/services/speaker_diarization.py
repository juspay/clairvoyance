"""
Speaker Diarization Service using Speechmatics STT with voice locking capabilities.
"""

import asyncio
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass
from pipecat.services.speechmatics.stt import SpeechmaticsSTTService
from pipecat.transcriptions.language import Language
from pipecat.frames.frames import TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.core.logger import logger
from app.core.config import (
    SPEECHMATICS_API_KEY,
    ENABLE_SPEAKER_DIARIZATION,
    SPEAKER_SENSITIVITY,
    MAX_SPEAKERS,
    ENABLE_VOICE_LOCKING
)


@dataclass
class SpeakerInfo:
    """Information about a detected speaker."""
    speaker_id: str
    first_detected_at: float
    total_speech_time: float
    is_locked: bool = False


class VoiceLockingProcessor(FrameProcessor):
    """
    Processor that implements voice locking functionality using Speechmatics diarization.
    Monitors transcription frames with speaker IDs and locks onto the first active speaker.
    """
    
    def __init__(self, 
                 enable_voice_locking: bool = True,
                 detection_window: float = 5.0,
                 min_speech_duration: float = 2.0):
        """
        Initialize the voice locking processor.
        
        Args:
            enable_voice_locking: Whether to enable voice locking
            detection_window: Time window to detect the primary speaker (seconds)
            min_speech_duration: Minimum speech duration to consider a speaker (seconds)
        """
        super().__init__()
        self.enable_voice_locking = enable_voice_locking
        self.detection_window = detection_window
        self.min_speech_duration = min_speech_duration
        
        self.speakers: Dict[str, SpeakerInfo] = {}
        self.locked_speaker: Optional[str] = None
        self.session_start_time: Optional[float] = None
        self.first_speaker_detected: Optional[str] = None
        
        logger.info(f"VoiceLockingProcessor initialized - locking: {enable_voice_locking}")
    
    async def process_frame(self, frame, direction: FrameDirection):
        """Process frames and implement voice locking logic."""
        frame_type = frame.__class__.__name__
        
        # Only log non-audio frames to reduce noise
        if frame_type != "UserAudioRawFrame":
            logger.info(f"[VOICE-LOCK-DEBUG] 📥 Received frame: {frame_type} | Direction: {direction}")
        
        await super().process_frame(frame, direction)
        
        if isinstance(frame, TranscriptionFrame):
            await self._handle_transcription_frame(frame)
        else:
            # Pass through non-transcription frames (but don't log audio frames)
            if frame_type != "UserAudioRawFrame":
                logger.info(f"[VOICE-LOCK-DEBUG] ⏭️  Passing through non-transcription frame: {frame_type}")
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)
    
    async def _handle_transcription_frame(self, frame: TranscriptionFrame):
        """Handle transcription frames with speaker filtering."""
        current_time = asyncio.get_event_loop().time()
        
        if self.session_start_time is None:
            self.session_start_time = current_time
            logger.info("[VOICE-LOCK-DEBUG] 🚀 Voice locking session started")
        
        # Extract speaker info from Speechmatics transcription
        speaker_id = getattr(frame, 'user_id', None)  # Speechmatics uses user_id for speaker
        transcription_text = getattr(frame, 'text', '')
        
        logger.info(f"[VOICE-LOCK-DEBUG] 🎤 Transcription: '{transcription_text}' | Speaker: {speaker_id} | Voice locking: {self.enable_voice_locking}")
        
        if speaker_id and self.enable_voice_locking:
            # Track speaker information
            if speaker_id not in self.speakers:
                self.speakers[speaker_id] = SpeakerInfo(
                    speaker_id=speaker_id,
                    first_detected_at=current_time,
                    total_speech_time=0.0
                )
                logger.info(f"[VOICE-LOCK-DEBUG] 👤 New speaker detected: {speaker_id}")
                
                # Lock onto the first speaker detected
                if self.first_speaker_detected is None:
                    self.first_speaker_detected = speaker_id
                    await self._lock_onto_speaker(speaker_id)
            
            # Update speech time (estimate based on transcript length)
            if transcription_text:
                estimated_duration = len(transcription_text.split()) * 0.6  # ~0.6 seconds per word
                self.speakers[speaker_id].total_speech_time += estimated_duration
                logger.debug(f"[VOICE-LOCK-DEBUG] ⏱️  Speaker {speaker_id} total time: {self.speakers[speaker_id].total_speech_time:.1f}s")
        
        # Filter frames based on voice locking
        if not self.enable_voice_locking:
            # Voice locking disabled, pass all frames
            logger.info(f"[VOICE-LOCK-DEBUG] ✅ Passing frame (voice locking disabled): '{transcription_text}'")
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)
        elif self.locked_speaker is None:
            # No locked speaker yet, pass all frames
            logger.info(f"[VOICE-LOCK-DEBUG] ✅ Passing frame (no locked speaker yet): '{transcription_text}'")
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)
        elif speaker_id == self.locked_speaker or speaker_id is None:
            # Frame from locked speaker or no speaker info, pass it
            logger.info(f"[VOICE-LOCK-DEBUG] ✅ Passing frame from locked speaker {speaker_id}: '{transcription_text}'")
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)
        else:
            # Frame from different speaker, filter it out
            logger.info(f"[VOICE-LOCK-DEBUG] 🚫 FILTERING frame from speaker {speaker_id} (locked on {self.locked_speaker}): '{transcription_text}'")
    
    async def _lock_onto_speaker(self, speaker_id: str):
        """Lock onto a specific speaker."""
        self.locked_speaker = speaker_id
        if speaker_id in self.speakers:
            self.speakers[speaker_id].is_locked = True
        
        logger.info(f"[VOICE-LOCK-DEBUG] 🔒 VOICE LOCKED onto speaker: {speaker_id}")
        logger.info(f"[VOICE-LOCK-DEBUG] 📊 Current speakers: {list(self.speakers.keys())}")
        logger.info(f"[VOICE-LOCK-DEBUG] 🎯 Locked speaker: {self.locked_speaker}")
        logger.info(f"[VOICE-LOCK-DEBUG] ⚡ Voice locking is now ACTIVE - filtering other speakers")
    
    def get_speaker_stats(self) -> Dict[str, Any]:
        """Get statistics about detected speakers."""
        return {
            "locked_speaker": self.locked_speaker,
            "first_speaker": self.first_speaker_detected,
            "total_speakers": len(self.speakers),
            "speakers": {
                sid: {
                    "first_detected_at": info.first_detected_at,
                    "total_speech_time": info.total_speech_time,
                    "is_locked": info.is_locked
                }
                for sid, info in self.speakers.items()
            }
        }


class SpeakerDiarizationService:
    """
    Service for creating Speechmatics STT with speaker diarization and voice locking.
    """
    
    def __init__(self):
        self.api_key = SPEECHMATICS_API_KEY
        if not self.api_key:
            raise ValueError("SPEECHMATICS_API_KEY is required for speaker diarization")
    
    def create_stt_service(self, 
                          languages: List[Language] = None,
                          enable_diarization: bool = None,
                          speaker_sensitivity: float = None,
                          max_speakers: int = None) -> SpeechmaticsSTTService:
        """
        Create a Speechmatics STT service with speaker diarization.
        
        Args:
            languages: List of languages to recognize
            enable_diarization: Whether to enable speaker diarization
            speaker_sensitivity: Sensitivity for speaker detection (0.0-1.0)
            max_speakers: Maximum number of speakers to detect
        
        Returns:
            Configured SpeechmaticsSTTService
        """
        if languages is None:
            languages = [Language.EN_US, Language.EN_IN]
        
        if enable_diarization is None:
            enable_diarization = ENABLE_SPEAKER_DIARIZATION
        
        if speaker_sensitivity is None:
            speaker_sensitivity = SPEAKER_SENSITIVITY
        
        if max_speakers is None:
            max_speakers = MAX_SPEAKERS
        
        logger.info(f"[VOICE-DEBUG] 🔧 Creating Speechmatics STT service...")
        logger.info(f"[VOICE-DEBUG] - Diarization enabled: {enable_diarization}")
        logger.info(f"[VOICE-DEBUG] - Speaker sensitivity: {speaker_sensitivity}")
        logger.info(f"[VOICE-DEBUG] - Max speakers: {max_speakers}")
        logger.info(f"[VOICE-DEBUG] - Languages: {[lang.name for lang in languages]}")
        
        # Configure speaker formats for better identification
        speaker_active_format = "<{speaker_id}>{text}</{speaker_id}>" if enable_diarization else None
        logger.info(f"[VOICE-DEBUG] - Speaker format: {speaker_active_format}")
        
        try:
            params = SpeechmaticsSTTService.InputParams(
                languages=languages,
                enable_diarization=enable_diarization,
                speaker_sensitivity=speaker_sensitivity,
                max_speakers=max_speakers,
                enable_interim_results=True,
                speaker_active_format=speaker_active_format,
                # Use focus_speakers for voice locking after first speaker is detected
                focus_speakers=[] if enable_diarization else None,
            )
            logger.info("[VOICE-DEBUG] ✅ Speechmatics parameters created successfully")
            
            stt_service = SpeechmaticsSTTService(
                api_key=self.api_key,
                params=params
            )
            logger.info("[VOICE-DEBUG] ✅ Speechmatics STT service created successfully")
            return stt_service
            
        except Exception as e:
            logger.error(f"[VOICE-DEBUG] ❌ Failed to create Speechmatics STT service: {e}")
            logger.error(f"[VOICE-DEBUG] ❌ Exception type: {type(e).__name__}")
            import traceback
            logger.error(f"[VOICE-DEBUG] ❌ Traceback: {traceback.format_exc()}")
            raise
    
    def create_voice_locking_processor(self, **kwargs) -> VoiceLockingProcessor:
        """Create a voice locking processor."""
        return VoiceLockingProcessor(
            enable_voice_locking=ENABLE_VOICE_LOCKING,
            **kwargs
        )
    
    def create_stt_with_voice_locking(self, **stt_kwargs) -> tuple[SpeechmaticsSTTService, VoiceLockingProcessor]:
        """
        Create STT service and voice locking processor as a pair.
        
        Returns:
            Tuple of (stt_service, voice_locking_processor)
        """
        stt_service = self.create_stt_service(**stt_kwargs)
        voice_processor = self.create_voice_locking_processor()
        
        return stt_service, voice_processor


# Global instance
speaker_diarization_service = SpeakerDiarizationService()
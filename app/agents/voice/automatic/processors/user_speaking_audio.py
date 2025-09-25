"""
User Speaking Audio Processor

Handles audio management based on user speaking events:
- User starts speaking → Enable audio
- User stops speaking → Start playing audio
- Clean, simple flow tied to user speech patterns
"""

import asyncio
import time
from typing import Optional
from pipecat.frames.frames import (
    Frame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
    EmulateUserStartedSpeakingFrame,
    EmulateUserStoppedSpeakingFrame,
    TranscriptionFrame,
    InterimTranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from app.core.logger import logger
from app.agents.voice.automatic.audio.audio_manager import get_audio_manager


class UserSpeakingAudioProcessor(FrameProcessor):
    """
    Processor that manages audio based on user speaking events.
    
    Flow:
    1. User starts speaking → Enable audio + start speech session timer
    2. User stops speaking → Check speech duration and start audio if reasonable duration
    3. Transcription received → Immediately mark as valid speech
    4. Bot starts speaking → Audio stops (handled elsewhere)
    """

    def __init__(self, name: str = "UserSpeakingAudioProcessor"):
        super().__init__(name=name)
        self._user_currently_speaking = False
        self._actual_speech_detected = False  # Track if transcription was received
        self._speech_start_time = None  # Track when speech started
        self._min_speech_duration = 2.0  # Minimum duration (seconds) for fallback (only for very long holds)
        self._pending_audio_task = None  # Task waiting for transcription
        self._transcription_timeout = 3.0  # Wait up to 3 seconds for transcription after PTT release

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames and manage audio based on user speaking events."""
        await super().process_frame(frame, direction)
        

        # Handle transcription frames - detect actual speech content
        if isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            # Accept transcription even after PTT release (delayed transcription)
            if frame.text.strip():
                if not self._actual_speech_detected:
                    self._actual_speech_detected = True
                    
                    # If we have a pending audio task waiting for transcription, start audio now
                    if self._pending_audio_task and not self._pending_audio_task.done():
                        # logger.info("Starting audio due to delayed transcription")
                        audio_manager = get_audio_manager()
                        if audio_manager:
                            await audio_manager.start_audio()
            
        # Handle user started speaking events
        elif isinstance(frame, (
            UserStartedSpeakingFrame,
            VADUserStartedSpeakingFrame,
            EmulateUserStartedSpeakingFrame
        )):
            if not self._user_currently_speaking:
                self._user_currently_speaking = True
                self._actual_speech_detected = False  # Reset speech detection for new session
                self._speech_start_time = time.time()  # Record when speech started
                audio_manager = get_audio_manager()
                if audio_manager:
                    # First stop any currently playing audio immediately
                    if audio_manager.is_playing:
                        await audio_manager.stop_and_disable_audio()
                        # logger.info("Stopped playing audio - user started speaking")
                    
                    # Then enable for new input
                    audio_manager.set_user_input()
                    # logger.info(f"Audio enabled - user started speaking ({type(frame).__name__})")
                else:
                    logger.error("No audio manager found!")

        # Handle user stopped speaking events
        elif isinstance(frame, (
            UserStoppedSpeakingFrame,
            VADUserStoppedSpeakingFrame,
            EmulateUserStoppedSpeakingFrame
        )):
            if self._user_currently_speaking:
                self._user_currently_speaking = False
                audio_manager = get_audio_manager()
                if audio_manager:
                    
                    # Check if transcription was already detected during speaking
                    if self._actual_speech_detected:
                        await audio_manager.start_audio()
                    else:
                        # Wait for delayed transcription (common case)
                        self._pending_audio_task = asyncio.create_task(self._wait_for_transcription())
                else:
                    logger.error("No audio manager found!")

        # Pass frame through to next processor
        await self.push_frame(frame, direction)
    
    async def _wait_for_transcription(self):
        """Wait for delayed transcription after PTT release."""
        try:
            # Wait for transcription timeout
            await asyncio.sleep(self._transcription_timeout)
            
            # Check if transcription arrived during wait
            if self._actual_speech_detected:
                logger.info("Audio started after transcription timeout - speech was detected")
                audio_manager = get_audio_manager()
                if audio_manager:
                    await audio_manager.start_audio()
            else:
                logger.info("No audio started - transcription timeout reached without speech detection")
                
        except asyncio.CancelledError:
            logger.debug("Transcription wait cancelled")
        except Exception as e:
            logger.error(f"Error in transcription wait: {e}")

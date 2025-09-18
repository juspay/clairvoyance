"""
User Speaking Audio Processor

Handles audio management based on user speaking events:
- User starts speaking → Enable audio
- User stops speaking → Start playing audio
- Clean, simple flow tied to user speech patterns
"""

import asyncio
from typing import Optional
from pipecat.frames.frames import (
    Frame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
    EmulateUserStartedSpeakingFrame,
    EmulateUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from app.core.logger import logger
from app.agents.voice.automatic.audio.audio_manager import get_audio_manager


class UserSpeakingAudioProcessor(FrameProcessor):
    """
    Processor that manages audio based on user speaking events.
    
    Flow:
    1. User starts speaking → Enable audio
    2. User stops speaking → Start playing audio
    3. Bot starts speaking → Audio stops (handled elsewhere)
    """

    def __init__(self, name: str = "UserSpeakingAudioProcessor"):
        super().__init__(name=name)
        self._user_currently_speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames and manage audio based on user speaking events."""
        await super().process_frame(frame, direction)
        
        # Debug: Log all frames to see what's coming through
        # logger.debug(f"UserSpeakingAudioProcessor received frame: {type(frame).__name__}")

        # Handle user started speaking events
        if isinstance(frame, (
            UserStartedSpeakingFrame,
            VADUserStartedSpeakingFrame,
            EmulateUserStartedSpeakingFrame
        )):
            logger.info(f"🎙️ USER STARTED SPEAKING DETECTED: {type(frame).__name__}")
            if not self._user_currently_speaking:
                self._user_currently_speaking = True
                audio_manager = get_audio_manager()
                if audio_manager:
                    # First stop any currently playing audio immediately
                    if audio_manager.is_playing:
                        await audio_manager.stop_and_disable_audio()
                        logger.info(f"🛑 Stopped playing audio - user started speaking")
                    
                    # Then enable for new input
                    audio_manager.enable_for_user_input()
                    logger.info(f"✅ Audio enabled - user started speaking ({type(frame).__name__})")
                else:
                    logger.error("❌ No audio manager found!")
            else:
                logger.info("User already marked as speaking")

        # Handle user stopped speaking events
        elif isinstance(frame, (
            UserStoppedSpeakingFrame,
            VADUserStoppedSpeakingFrame,
            EmulateUserStoppedSpeakingFrame
        )):
            logger.info(f"🔇 USER STOPPED SPEAKING DETECTED: {type(frame).__name__}")
            if self._user_currently_speaking:
                self._user_currently_speaking = False
                audio_manager = get_audio_manager()
                if audio_manager:
                    # Start audio immediately when user stops speaking
                    await audio_manager.start_for_user_input()
                    logger.info(f"🎵 Audio started - user stopped speaking ({type(frame).__name__})")
                else:
                    logger.error("❌ No audio manager found!")
            else:
                logger.info("User already marked as not speaking")

        # Pass frame through to next processor
        await self.push_frame(frame, direction)
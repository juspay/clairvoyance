"""
Improved Audio Manager with dynamic duration detection and proper looping
"""

import time
import asyncio
from typing import Optional
from pydub import AudioSegment
from pipecat.frames.frames import OutputAudioRawFrame
from app.core.logger import logger

# Configurable audio length constant - change this to test different audio lengths
AUDIO_LENGTH_SECONDS = 1  # Default duration in seconds


class AudioManager:
    """Audio manager that plays waiting audio with proper looping based on actual audio duration."""
    
    def __init__(self, tts_service, transport=None):
        self.tts_service = tts_service
        self.transport = transport  # Keep for future use if needed
        self.waiting_audio_data = None
        self.audio_duration_seconds = AUDIO_LENGTH_SECONDS
        self.is_playing = False
        self.loop_task: Optional[asyncio.Task] = None
        
        # New simplified state management
        self.audio_enabled = False  # Enable when user starts speaking
        self.user_is_speaking = False  # Track if user is currently speaking
        
        # Legacy flags (keep for compatibility during transition)
        self.user_input_active = False
        self.stop_requested = False
        self.response_started = False
        self.audio_played_for_current_input = False
        self.bot_speaking = False
        self.function_calls_active = False
        self._load_waiting_audio()
    
    def _load_waiting_audio(self):
        f"""Load the {AUDIO_LENGTH_SECONDS}-second waiting audio."""
        try:
            wav_file_path = f"app/agents/voice/automatic/audio/waiting_{int(AUDIO_LENGTH_SECONDS)}sec.wav"

            
            audio = AudioSegment.from_wav(wav_file_path)
            # Convert to pipeline format
            audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
            self.waiting_audio_data = audio.raw_data
            
            # Set duration from constant
            self.audio_duration_seconds = AUDIO_LENGTH_SECONDS
            
            logger.info(f"Loaded {AUDIO_LENGTH_SECONDS}-second waiting audio: {len(self.waiting_audio_data)} bytes")

        except Exception as e:
            logger.error(f"Failed to load waiting_{int(AUDIO_LENGTH_SECONDS)}sec.wav: {e}")
            self.waiting_audio_data = None
            self.audio_duration_seconds = AUDIO_LENGTH_SECONDS
    
    async def start_for_user_input(self):
        """Start waiting audio for a new user input (only once per input)."""
       
        # Check if we should skip audio start
        blocking_conditions = {
            'user_input_active': self.user_input_active,
            'no_audio_data': not self.waiting_audio_data,
            'response_started': self.response_started,
            'audio_played_for_current_input': self.audio_played_for_current_input,
            'bot_speaking': self.bot_speaking,
            'function_calls_active': self.function_calls_active,
            'is_playing': self.is_playing
        }
        
        blocking_flags = [k for k, v in blocking_conditions.items() if v]
        
        if blocking_flags:
            logger.warning(f"🚫 AUDIO BLOCKED by: {', '.join(blocking_flags)}")
            logger.info(f"Full state: {blocking_conditions}")
            return
        
        self.user_input_active = True
        self.is_playing = True
        self.stop_requested = False
        self.response_started = False
        self.function_calls_active = True  # Mark function calls as active
        self.audio_played_for_current_input = True  # Mark that audio is playing for this input
        
        # Add small delay to prevent immediate audio playback
        await asyncio.sleep(0.3)
        
        # Check again if we should still play (conditions might have changed)
        if self.stop_requested or self.response_started or self.bot_speaking:
            logger.info("⚠️ Conditions changed during delay, not starting audio")
            self.user_input_active = False
            self.is_playing = False
            self.function_calls_active = False
            return
            
        # Use the new improved loop instead of old one
        self.loop_task = asyncio.create_task(self._loop_until_bot_speaks())
        logger.info("Started waiting audio loop (will loop until response)")
    
    async def stop_for_response(self):
        """Stop waiting audio when response is ready - simple and effective."""
        if not self.user_input_active or self.stop_requested:
            return
        
        self.stop_requested = True
        self.user_input_active = False
        self.is_playing = False
        self.response_started = True  # Mark that response has started
        self.function_calls_active = False  # Reset function calls state
        
        if self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()
            try:
                await self.loop_task
            except asyncio.CancelledError:
                pass
        
        # Only log once to reduce spam
        logger.info("Stopped waiting audio - response ready")
    
    async def stop_waiting_audio_loop(self):
        """Alias for stop_for_response to maintain compatibility."""
        await self.stop_for_response()
    
    def enable_for_user_input(self):
        """Enable audio when user starts speaking - NEW METHOD"""
        
        # Force reset all blocking flags to ensure audio can start
        self._force_reset_blocking_flags()
        
        self.audio_enabled = True
        self.user_is_speaking = True
        self.is_playing = False
        self.stop_requested = False
        
        # Cancel any existing tasks
        if self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()
        
    
    def _force_reset_blocking_flags(self):
        """Force reset all flags that could block audio from starting"""

        
        # Reset the critical user_input_active flag that was blocking audio
        self.user_input_active = False
        self.response_started = False
        self.audio_played_for_current_input = False
        self.bot_speaking = False
        self.function_calls_active = False
        
    
    async def start_playing_audio(self):
        """Start playing audio when user stops speaking - NEW METHOD"""
        
        if not self.audio_enabled:
            logger.info("Audio not enabled - skipping")
            return
        if self.is_playing:
            logger.info("Audio already playing - skipping")
            return
        if not self.waiting_audio_data:
            logger.info("No waiting audio data - skipping")
            return
        
        self.user_is_speaking = False
        self.is_playing = True
        self.stop_requested = False  # Ensure stop flag is clear
        self.loop_task = asyncio.create_task(self._loop_until_bot_speaks())
        logger.info("Started playing audio - user stopped speaking - will loop continuously until bot responds")
    
    async def stop_and_disable_audio(self):
        """Stop audio and disable until next user input - NEW METHOD"""
        logger.info(f"🛑 STOP_AND_DISABLE_AUDIO called - current state: enabled={self.audio_enabled}, playing={self.is_playing}, task_running={self.loop_task and not self.loop_task.done() if self.loop_task else False}")
        
        self.audio_enabled = False
        self.is_playing = False
        self.user_is_speaking = False
        self.stop_requested = True
        
        if self.loop_task and not self.loop_task.done():
            logger.info("🔄 Cancelling audio loop task...")
            self.loop_task.cancel()
            try:
                await self.loop_task
                logger.info("✅ Audio loop task cancelled successfully")
            except asyncio.CancelledError:
                logger.info("✅ Audio loop task cancellation confirmed")
        
        logger.info("🛑 Audio stopped and disabled - bot speaking")
    
    def reset_for_new_input(self):
        """Reset the audio manager for a new user input cycle - LEGACY METHOD"""
        # Don't reset if audio is currently enabled and should be playing
        # This prevents premature resets during function calls
        if self.audio_enabled and not self.bot_speaking and not self.response_started:
            logger.info("🚫 Skipping reset - audio is enabled and should be playing")
            return
            
        # Stop any active audio first
        if self.is_playing or self.user_input_active:
            self.stop_requested = True
            self.is_playing = False
            self.user_input_active = False
            if self.loop_task and not self.loop_task.done():
                self.loop_task.cancel()
        
        # Reset all state flags
        self.response_started = False
        self.audio_played_for_current_input = False
        self.bot_speaking = False
        self.function_calls_active = False
        self.stop_requested = False
        
        # Reset new flags too
        self.audio_enabled = False
        self.user_is_speaking = False
        
        logger.info("Reset audio manager for new user input - all state cleared")
    
    def set_bot_speaking(self, speaking: bool):
        """Track when bot starts/stops speaking to prevent audio during speech."""
        self.bot_speaking = speaking
        if speaking and self.is_playing:
            # Stop audio immediately when bot starts speaking
            asyncio.create_task(self.stop_for_response())
            logger.debug("Stopped audio - bot started speaking")
    
    async def _loop_until_response(self):
        """Loop waiting audio until response arrives (but only start once per user input)."""
        try:
            while self.is_playing and self.user_input_active and not self.stop_requested:
                if self.waiting_audio_data:
                    # Create and queue audio frame
                    audio_frame = OutputAudioRawFrame(
                        audio=self.waiting_audio_data,
                        sample_rate=16000,
                        num_channels=1
                    )
                    await self.tts_service.queue_frame(audio_frame)
                    logger.debug("Playing waiting audio (will loop until response)")
                    
                    # Wait 4 seconds for audio to finish, then loop seamlessly
                    # Check every 0.1 seconds if we should stop
                    for _ in range(40):  # 40 * 0.1 = 4 seconds
                        if not self.is_playing or self.stop_requested:
                            return
                        await asyncio.sleep(0.1)
                else:
                    await asyncio.sleep(0.1)
                    
        except asyncio.CancelledError:
            pass  # Don't log cancellation - it's expected
        except Exception as e:
            logger.error(f"Error in audio loop: {e}")
        finally:
            self.is_playing = False
            self.user_input_active = False
            self.function_calls_active = False
            # Don't reset response_started here - let it be reset by new user input
    
    async def _loop_until_bot_speaks(self):
        """Loop waiting audio until response arrives (max 10 loops)."""
        loop_count = 0
        MAX_LOOPS = 10
        logger.info(f"isPlaying>> {self.is_playing}, stopRequested>> {self.stop_requested}, maxLoops>> {MAX_LOOPS}, loopCount>> {loop_count},{self.is_playing and not self.stop_requested and loop_count < MAX_LOOPS}")

        try:
            # logger.info(f"isPlaying>>33 {self.is_playing}, stopRequested>> {self.stop_requested}, maxLoops>> {MAX_LOOPS}, loopCount>> {loop_count},{self.is_playing and not self.stop_requested and loop_count < MAX_LOOPS}")
            while self.is_playing and not self.stop_requested and loop_count < MAX_LOOPS:

                if self.waiting_audio_data:
                    logger.info(f"if self.waiting_audio_data>>")
                    loop_count += 1
                    logger.info(f"loop_count incremented to {loop_count}>>")

                    # Create and queue audio frame
                    audio_frame = OutputAudioRawFrame(
                        audio=self.waiting_audio_data,
                        sample_rate=16000,
                        num_channels=1
                    )
                    await self.tts_service.queue_frame(audio_frame)
                    logger.info(f"🔊 Playing waiting audio (loop #{loop_count}/{MAX_LOOPS})")
                    
                    # Wait for audio duration, checking every 0.1 seconds if we should stop
                    for _ in range(int(self.audio_duration_seconds * 10)):  # Check every 0.1 seconds
                        if not self.is_playing or self.stop_requested:
                            return
                        await asyncio.sleep(0.1)
                else:
                    logger.info(f"else self.waiting_audio_data>>22")
                    await asyncio.sleep(0.1)
            
            # If we completed all loops without being stopped
                if loop_count >= MAX_LOOPS:
                    logger.info(f"🏁 Completed all {MAX_LOOPS} loops - stopping naturally")
            
                    
        except asyncio.CancelledError:
            pass  # Don't log cancellation - it's expected
        except Exception as e:
            logger.error(f"Error in audio loop: {e}")
        finally:
            self.is_playing = False


# Global audio manager instance
_audio_manager: Optional[AudioManager] = None


def get_audio_manager() -> Optional[AudioManager]:
    """Get the global audio manager instance."""
    return _audio_manager


def set_audio_manager(audio_manager: AudioManager):
    """Set the global audio manager instance."""
    global _audio_manager
    _audio_manager = audio_manager


def initialize_audio_manager(tts_service, transport=None) -> AudioManager:
    """Initialize and return the audio manager."""
    audio_manager = AudioManager(tts_service, transport)
    set_audio_manager(audio_manager)
    return audio_manager


# Simple helper function to stop audio from anywhere
async def stop_audio_immediately():
    """Simple function to stop audio immediately from anywhere in the code."""
    audio_manager = get_audio_manager()
    if audio_manager:
        await audio_manager.stop_for_response()

def set_bot_speaking_state(speaking: bool):
    """Global function to set bot speaking state from anywhere."""
    audio_manager = get_audio_manager()
    if audio_manager:
        audio_manager.set_bot_speaking(speaking)
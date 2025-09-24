"""
Simplified Audio Manager - Minimal variables, seamless playback, immediate stop capability
"""

import asyncio
from typing import Optional
from pydub import AudioSegment
from pipecat.frames.frames import OutputAudioRawFrame
from app.core.logger import logger

# Configurable audio length constant
AUDIO_LENGTH_SECONDS = 6  # Default duration in seconds


class AudioManager:
    """Simplified audio manager with minimal state and seamless chunked playback."""
    
    def __init__(self, tts_service, transport=None):
        self.tts_service = tts_service
        self.transport = transport  # Keep for compatibility
        
        # MINIMAL STATE - Only 4 variables needed
        self.is_playing = False
        self.loop_count = 0  # Track completed loops (max 3 = 18 seconds total)
        self.audio_chunks = []  # 1-second audio chunks for seamless playback
        self.user_has_input = False  # Ensure audio only starts after user input
        
        # Internal task management
        self.loop_task: Optional[asyncio.Task] = None
        
        self._load_waiting_audio()
    
    def _load_waiting_audio(self):
        """Load waiting audio and split into 1-second chunks for seamless playback."""
        try:
            wav_file_path = f"app/agents/voice/automatic/audio/waiting_{int(AUDIO_LENGTH_SECONDS)}sec.wav"
            
            audio = AudioSegment.from_wav(wav_file_path)
            # Convert to pipeline format
            audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
            
            # Split into clean 1-second chunks
            self.audio_chunks = []
            chunk_duration_ms = 1000  # 1 second in milliseconds
            
            for i in range(0, len(audio), chunk_duration_ms):
                chunk = audio[i:i + chunk_duration_ms]
                
                # Ensure chunk is exactly 1 second (pad if necessary)
                if len(chunk) < chunk_duration_ms:
                    silence_needed = chunk_duration_ms - len(chunk)
                    silence = AudioSegment.silent(duration=silence_needed, frame_rate=16000)
                    chunk = chunk + silence
                
                self.audio_chunks.append(chunk.raw_data)
            
            logger.info(f"Loaded {AUDIO_LENGTH_SECONDS}-second waiting audio: {len(self.audio_chunks)} chunks")

        except Exception as e:
            logger.error(f"Failed to load waiting_{int(AUDIO_LENGTH_SECONDS)}sec.wav: {e}")
            self.audio_chunks = []
    
    def set_user_input(self):
        """Mark that user has provided input - required for audio to start."""
        self.user_has_input = True
        logger.info("🎤 User input detected - audio enabled")
    
    async def start_audio(self):
        """Start seamless chunked audio playback (only if user has provided input)."""
        
        # Check prerequisites
        if not self.user_has_input:
            logger.info("🚫 No user input detected - audio blocked")
            return
            
        if not self.audio_chunks:
            logger.warning("🚫 No audio chunks available")
            return
            
        if self.is_playing:
            logger.info("🔄 Audio already playing - skipping")
            return
        
        # Start audio
        self.is_playing = True
        self.loop_count = 0
        
        # Cancel any existing task
        if self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()
        
        # Start seamless audio streaming
        self.loop_task = asyncio.create_task(self._stream_seamless_audio())
        logger.info(f"🎵 Started seamless audio - max 3 loops (18s total)")
    
    async def stop_and_disable_audio(self):
        """Stop audio immediately and disable until next user input."""
        logger.info("🛑 Stopping audio immediately")
        
        # Set stop flags
        self.is_playing = False
        self.user_has_input = False  # Require new user input for next audio
        self.loop_count = 0
        
        # Cancel audio task
        if self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()
            try:
                await self.loop_task
            except asyncio.CancelledError:
                pass
        
        # Clear audio queue aggressively
        await self._clear_audio_queue()
        
        logger.info("✅ Audio stopped and disabled")
    
    def reset(self):
        """Reset for new conversation cycle."""
        self.is_playing = False
        self.user_has_input = False
        self.loop_count = 0
        
        if self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()
        
        logger.info("🔄 Audio manager reset for new conversation")
    
    def set_bot_speaking(self, speaking: bool):
        """Track when bot starts/stops speaking to prevent audio during speech."""
        if speaking and self.is_playing:
            logger.info("🛑 Bot started speaking - stopping audio")
            asyncio.create_task(self.stop_and_disable_audio())
    
    async def _stream_seamless_audio(self):
        """Stream audio chunks seamlessly with minimal buffer for immediate stopping."""
        try:
            if not self.audio_chunks:
                return
            
            total_chunks = len(self.audio_chunks)  # 6 chunks = 6 seconds
            max_loops = 3  # Maximum 3 loops = 18 seconds total
            chunk_index = 0
            
            logger.info(f"🚀 Starting seamless streaming: {total_chunks} chunks, max {max_loops} loops")
            
            # Pre-queue 1 chunk for seamless start (minimal buffer)
            if self.is_playing:
                audio_frame = OutputAudioRawFrame(
                    audio=self.audio_chunks[chunk_index],
                    sample_rate=16000,
                    num_channels=1
                )
                await self.tts_service.queue_frame(audio_frame)
                logger.info(f"🔊 Pre-queued chunk 1/{total_chunks}")
                chunk_index = 1
            
            # Continue streaming with optimal timing for seamless playback
            while self.is_playing and self.loop_count < max_loops:
                
                # Wait 0.8 seconds before queuing next chunk (seamless timing)
                await asyncio.sleep(0.8)
                
                # Check if we should stop
                if not self.is_playing:
                    break
                
                # Queue next chunk
                current_chunk = self.audio_chunks[chunk_index % total_chunks]
                audio_frame = OutputAudioRawFrame(
                    audio=current_chunk,
                    sample_rate=16000,
                    num_channels=1
                )
                await self.tts_service.queue_frame(audio_frame)
                
                chunk_index += 1
                
                # Update loop count when we complete a full cycle
                if chunk_index % total_chunks == 0:
                    self.loop_count += 1
                    logger.info(f"🔄 Completed loop {self.loop_count}/{max_loops}")
                
                logger.debug(f"🔊 Queued chunk {(chunk_index % total_chunks) + 1}/{total_chunks}")
            
            # Natural completion
            if self.loop_count >= max_loops:
                logger.info(f"🏁 Completed maximum {max_loops} loops - stopping naturally")
            
        except asyncio.CancelledError:
            logger.info("🛑 Audio streaming cancelled")
        except Exception as e:
            logger.error(f"❌ Error in audio streaming: {e}")
        finally:
            self.is_playing = False
            logger.info("🔄 Audio streaming finished")
    
    async def _clear_audio_queue(self):
        """Clear audio queue to prevent delayed audio after stop."""
        try:
            # Try to interrupt TTS service
            if hasattr(self.tts_service, 'interrupt'):
                if asyncio.iscoroutinefunction(self.tts_service.interrupt):
                    await self.tts_service.interrupt()
                else:
                    self.tts_service.interrupt()
                logger.info("🚨 Interrupted TTS service")
            
            # Send brief silence to flush pipeline
            silence_duration = 0.01  # 10ms silence
            sample_rate = 16000
            silence_samples = int(silence_duration * sample_rate)
            silence_data = b'\x00' * (silence_samples * 2)  # 16-bit silence
            
            silence_frame = OutputAudioRawFrame(
                audio=silence_data,
                sample_rate=16000,
                num_channels=1
            )
            await self.tts_service.queue_frame(silence_frame)
            logger.info("🔇 Queued silence to flush audio pipeline")
            
        except Exception as e:
            logger.debug(f"Audio queue clearing failed: {e}")


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


# Simple helper functions for external use
async def stop_audio_immediately():
    """Stop audio immediately from anywhere in the code."""
    audio_manager = get_audio_manager()
    if audio_manager:
        await audio_manager.stop_and_disable_audio()


def set_bot_speaking_state(speaking: bool):
    """Set bot speaking state from anywhere."""
    audio_manager = get_audio_manager()
    if audio_manager:
        audio_manager.set_bot_speaking(speaking)


def mark_user_input():
    """Mark that user has provided input - enables audio."""
    audio_manager = get_audio_manager()
    if audio_manager:
        audio_manager.set_user_input()


# Legacy compatibility methods (simplified)
async def start_for_user_input():
    """Legacy method - start audio for user input."""
    audio_manager = get_audio_manager()
    if audio_manager:
        audio_manager.set_user_input()
        await audio_manager.start_audio()


async def stop_for_response():
    """Legacy method - stop audio for response."""
    await stop_audio_immediately()


def reset_for_new_input():
    """Legacy method - reset for new input."""
    audio_manager = get_audio_manager()
    if audio_manager:
        audio_manager.reset()

"""
Latency Tracking Processors for Breeze Buddy Voice Agent

Provides frame processors that integrate with LatencyTracker to measure
latency at each stage of the voice pipeline (STT, LLM, TTS).
"""

import time
from typing import Callable, Optional

from loguru import logger
from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMRunFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.ai.voice.agents.breeze_buddy.utils.latency_tracker import LatencyTracker
from app.core.config import static


class STTLatencyProcessor(FrameProcessor):
    """
    Tracks Speech-to-Text latency.

    Measures:
    - Time from first audio input to first transcription result (TTFB)
    - Total time from audio input to final transcription
    """

    def __init__(
        self,
        tracker: LatencyTracker,
        turn_id_provider: Callable[[], str],
        name: str = "STTLatencyProcessor"
    ):
        """
        Initialize STT latency processor.

        Args:
            tracker: LatencyTracker instance
            turn_id_provider: Function that returns current turn ID
            name: Processor name for logging
        """
        super().__init__(name=name)
        self.tracker = tracker
        self.turn_id_provider = turn_id_provider
        self.stt_start_time: Optional[float] = None
        self.first_result_time: Optional[float] = None
        self.audio_frames_count = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames and track STT latency."""

        # Track when audio input starts (first audio frame of turn)
        if isinstance(frame, AudioRawFrame):
            if self.stt_start_time is None:
                self.stt_start_time = time.time()
                self.audio_frames_count = 0
                logger.trace(f"[STT Latency] Audio input started")

            self.audio_frames_count += 1

        # Track first transcription result (interim or final)
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            if self.first_result_time is None and self.stt_start_time:
                self.first_result_time = time.time()
                ttfb = (self.first_result_time - self.stt_start_time) * 1000
                logger.debug(
                    f"[STT Latency] First result received: {ttfb:.0f}ms, "
                    f"interim={isinstance(frame, InterimTranscriptionFrame)}, "
                    f"text_len={len(frame.text)}"
                )

            # Track on final transcription
            if isinstance(frame, TranscriptionFrame):
                turn_id = self.turn_id_provider()

                if self.stt_start_time and turn_id:
                    total_duration = (time.time() - self.stt_start_time) * 1000
                    first_byte_latency = (
                        (self.first_result_time - self.stt_start_time) * 1000
                        if self.first_result_time
                        else None
                    )

                    self.tracker.track_component(
                        "stt",
                        first_byte_latency_ms=first_byte_latency,
                        total_duration_ms=total_duration,
                        turn_id=turn_id,
                        metadata={
                            "provider": "soniox",
                            "model": static.SONIOX_MODEL,
                            "interim_enabled": getattr(static, 'SONIOX_ENABLE_NON_FINAL_TOKENS', False),
                            "audio_frames": self.audio_frames_count,
                            "transcript_length": len(frame.text),
                        }
                    )

                    logger.info(
                        f"[STT Latency] Turn {turn_id}: "
                        f"TTFB={first_byte_latency:.0f}ms, "
                        f"total={total_duration:.0f}ms, "
                        f"transcript='{frame.text[:50]}...'"
                    )

                # Reset for next turn
                self.stt_start_time = None
                self.first_result_time = None
                self.audio_frames_count = 0

        await self.push_frame(frame, direction)


class LLMLatencyProcessor(FrameProcessor):
    """
    Tracks Language Model latency.

    Measures:
    - Time from LLM request to first token (TTFB)
    - Total time for complete LLM response
    """

    def __init__(
        self,
        tracker: LatencyTracker,
        turn_id_provider: Callable[[], str],
        name: str = "LLMLatencyProcessor"
    ):
        """
        Initialize LLM latency processor.

        Args:
            tracker: LatencyTracker instance
            turn_id_provider: Function that returns current turn ID
            name: Processor name for logging
        """
        super().__init__(name=name)
        self.tracker = tracker
        self.turn_id_provider = turn_id_provider
        self.llm_start_time: Optional[float] = None
        self.first_token_time: Optional[float] = None
        self.current_turn_id: Optional[str] = None
        self.tokens_count = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames and track LLM latency."""

        # Start tracking on LLM run
        if isinstance(frame, LLMRunFrame):
            self.llm_start_time = time.time()
            self.first_token_time = None
            self.tokens_count = 0

            # Get or generate turn ID
            self.current_turn_id = self.turn_id_provider()

            # Start turn tracking
            if self.tracker and self.current_turn_id:
                self.tracker.start_turn(self.current_turn_id)

            logger.trace(f"[LLM Latency] LLM run started for turn {self.current_turn_id}")

        # Track first token (when TTS receives first text to speak)
        elif isinstance(frame, TTSSpeakFrame):
            if self.first_token_time is None and self.llm_start_time:
                self.first_token_time = time.time()
                ttfb = (self.first_token_time - self.llm_start_time) * 1000
                logger.debug(
                    f"[LLM Latency] First token received: {ttfb:.0f}ms, "
                    f"text='{frame.text[:50]}...'"
                )

            self.tokens_count += 1

        # Track completion
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self.tracker and self.llm_start_time and self.current_turn_id:
                total_duration = (time.time() - self.llm_start_time) * 1000
                first_byte_latency = (
                    (self.first_token_time - self.llm_start_time) * 1000
                    if self.first_token_time
                    else None
                )

                self.tracker.track_component(
                    "llm",
                    first_byte_latency_ms=first_byte_latency,
                    total_duration_ms=total_duration,
                    turn_id=self.current_turn_id,
                    metadata={
                        "model": static.AZURE_OPENAI_MODEL,
                        "provider": "azure_openai",
                        "tokens_count": self.tokens_count,
                    }
                )

                logger.info(
                    f"[LLM Latency] Turn {self.current_turn_id}: "
                    f"TTFB={first_byte_latency:.0f}ms, "
                    f"total={total_duration:.0f}ms, "
                    f"tokens={self.tokens_count}"
                )

            # Reset for next turn
            self.llm_start_time = None
            self.first_token_time = None
            self.current_turn_id = None
            self.tokens_count = 0

        await self.push_frame(frame, direction)


class TTSLatencyProcessor(FrameProcessor):
    """
    Tracks Text-to-Speech latency.

    Measures:
    - Time from TTS start to first audio chunk (TTFB)
    - Total time for complete TTS synthesis
    """

    def __init__(
        self,
        tracker: LatencyTracker,
        turn_id_provider: Callable[[], str],
        name: str = "TTSLatencyProcessor"
    ):
        """
        Initialize TTS latency processor.

        Args:
            tracker: LatencyTracker instance
            turn_id_provider: Function that returns current turn ID
            name: Processor name for logging
        """
        super().__init__(name=name)
        self.tracker = tracker
        self.turn_id_provider = turn_id_provider
        self.tts_start_time: Optional[float] = None
        self.first_audio_time: Optional[float] = None
        self.audio_chunks_count = 0
        self.total_audio_bytes = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames and track TTS latency."""

        # Track when TTS starts
        if isinstance(frame, TTSStartedFrame):
            self.tts_start_time = time.time()
            self.first_audio_time = None
            self.audio_chunks_count = 0
            self.total_audio_bytes = 0
            logger.trace(f"[TTS Latency] TTS started")

        # Track first audio output
        elif isinstance(frame, TTSAudioRawFrame):
            if self.first_audio_time is None and self.tts_start_time:
                self.first_audio_time = time.time()
                ttfb = (self.first_audio_time - self.tts_start_time) * 1000
                logger.debug(f"[TTS Latency] First audio chunk received: {ttfb:.0f}ms")

            self.audio_chunks_count += 1
            self.total_audio_bytes += len(frame.audio)

        # Track completion
        elif isinstance(frame, TTSStoppedFrame):
            turn_id = self.turn_id_provider()

            if self.tracker and self.tts_start_time and turn_id:
                total_duration = (time.time() - self.tts_start_time) * 1000
                first_byte_latency = (
                    (self.first_audio_time - self.tts_start_time) * 1000
                    if self.first_audio_time
                    else None
                )

                self.tracker.track_component(
                    "tts",
                    first_byte_latency_ms=first_byte_latency,
                    total_duration_ms=total_duration,
                    turn_id=turn_id,
                    metadata={
                        "provider": "elevenlabs",
                        "model": static.ELEVENLABS_MODEL_ID,
                        "voice_id": static.ELEVENLABS_VOICE_ID,
                        "audio_chunks": self.audio_chunks_count,
                        "total_bytes": self.total_audio_bytes,
                    }
                )

                logger.info(
                    f"[TTS Latency] Turn {turn_id}: "
                    f"TTFB={first_byte_latency:.0f}ms, "
                    f"total={total_duration:.0f}ms, "
                    f"chunks={self.audio_chunks_count}"
                )

                # End turn tracking
                self.tracker.end_turn(turn_id)

            # Reset
            self.tts_start_time = None
            self.first_audio_time = None
            self.audio_chunks_count = 0
            self.total_audio_bytes = 0

        await self.push_frame(frame, direction)


def create_latency_processors(
    tracker: LatencyTracker,
    turn_id_provider: Callable[[], str]
) -> tuple[STTLatencyProcessor, LLMLatencyProcessor, TTSLatencyProcessor]:
    """
    Create all three latency tracking processors.

    Args:
        tracker: LatencyTracker instance
        turn_id_provider: Function that returns current turn ID

    Returns:
        Tuple of (STTLatencyProcessor, LLMLatencyProcessor, TTSLatencyProcessor)

    Example:
        >>> tracker = LatencyTracker(session_id="session_123")
        >>> get_turn_id = lambda: f"turn_{int(time.time())}"
        >>> stt_proc, llm_proc, tts_proc = create_latency_processors(tracker, get_turn_id)
        >>>
        >>> # Add to pipeline
        >>> pipeline = Pipeline([
        >>>     transport.input(),
        >>>     stt_proc,  # Track STT latency
        >>>     stt_service,
        >>>     llm_proc,  # Track LLM latency
        >>>     llm_service,
        >>>     tts_proc,  # Track TTS latency
        >>>     tts_service,
        >>>     transport.output(),
        >>> ])
    """
    stt_processor = STTLatencyProcessor(tracker, turn_id_provider)
    llm_processor = LLMLatencyProcessor(tracker, turn_id_provider)
    tts_processor = TTSLatencyProcessor(tracker, turn_id_provider)

    logger.info("Latency tracking processors created")

    return stt_processor, llm_processor, tts_processor

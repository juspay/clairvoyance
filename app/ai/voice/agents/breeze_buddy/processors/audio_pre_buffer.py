"""Audio pre-buffer processor for Daily WebRTC output.

Buffers the first N audio frames from TTS before releasing them downstream
to the transport output. This gives the Daily SDK's internal WebRTC buffer
a head start, preventing play-cursor starvation during the initial burst of
TTS audio.

Once the pre-buffer is full, all buffered frames are flushed at once, and
subsequent frames pass through immediately with no additional latency.

This processor is only useful for Daily (WebRTC) mode where the
without_mixer audio path has no built-in pacing or jitter buffer.
"""

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    Frame,
    OutputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor



class AudioPreBufferProcessor(FrameProcessor):
    """Buffers the first N audio frames per bot turn, then passes through.

    Pipeline position: between TTS and transport.output()
        ... → tts → AudioPreBufferProcessor → transport.output() → ...

    The buffer resets at the start of each new bot speaking turn so that
    every response gets the same initial runway.
    """

    def __init__(self, pre_buffer_count: int = 3, **kwargs):
        super().__init__(**kwargs)
        self._pre_buffer_count = pre_buffer_count
        self._buffer: list[OutputAudioRawFrame] = []
        self._buffering = True

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, OutputAudioRawFrame):
            if self._buffering:
                self._buffer.append(frame)
                if len(self._buffer) >= self._pre_buffer_count:
                    # Flush all buffered frames at once
                    for buffered_frame in self._buffer:
                        await self.push_frame(buffered_frame, direction)
                    self._buffer.clear()
                    self._buffering = False
                # Don't push this frame yet while buffering
                return
            # After initial buffer filled, pass through immediately
            await self.push_frame(frame, direction)

        elif isinstance(frame, BotStoppedSpeakingFrame):
            # Reset for next turn — buffer the start of the next response
            if self._buffer:
                # Flush any remaining buffered frames (edge case: turn ended
                # before buffer was full)
                for buffered_frame in self._buffer:
                    await self.push_frame(buffered_frame, direction)
                self._buffer.clear()
            self._buffering = True
            await self.push_frame(frame, direction)

        else:
            # All non-audio, non-reset frames pass through immediately
            await self.push_frame(frame, direction)

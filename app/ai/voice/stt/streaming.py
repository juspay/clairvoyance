"""Realtime (streaming) speech-to-text.

Counterpart to :mod:`transcribe` (one-shot clips): this module drives the same
*streaming* Pipecat STT services the voice pipeline uses, but without a full
agent pipeline. A :class:`StreamingTranscriber` wraps a minimal two-node
pipeline — provider STT service → transcript emitter — is fed raw PCM16 mono
audio chunks, and reports interim/final transcripts through an async callback
as the provider produces them.

Only continuously-streaming services work here (Soniox, Deepgram, Sarvam,
Google). Segmented services (OpenAI Whisper) need VAD frames that this
minimal pipeline does not produce.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.core.logger import logger

__all__ = ["StreamingTranscriber", "TranscriptEvent"]

_STOP_TIMEOUT_SECONDS = 5.0

# How long start() lets the pipeline run before declaring it healthy. Provider
# connect/auth rejections typically surface within this window (as an
# ErrorFrame or the runner task finishing), letting callers reject the stream
# before telling the client it is ready. Healthy streams pay this once at
# startup.
_STARTUP_GRACE_SECONDS = 1.0


@dataclass
class TranscriptEvent:
    """A single interim or final transcript produced during a stream."""

    text: str
    is_final: bool
    language: Optional[str] = None


TranscriptCallback = Callable[[TranscriptEvent], Awaitable[None]]


class _TranscriptEmitter(FrameProcessor):
    """Sink forwarding interim/final transcription frames to a callback.

    Callback errors are logged and swallowed so a slow or broken consumer can
    never wedge the STT service's frame loop.
    """

    def __init__(self, on_transcript: TranscriptCallback, **kwargs):
        super().__init__(**kwargs)
        self._on_transcript = on_transcript

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if direction == FrameDirection.DOWNSTREAM:
            event: Optional[TranscriptEvent] = None
            if isinstance(frame, InterimTranscriptionFrame) and frame.text:
                event = TranscriptEvent(
                    text=frame.text,
                    is_final=False,
                    language=str(frame.language) if frame.language else None,
                )
            elif isinstance(frame, TranscriptionFrame) and frame.text:
                event = TranscriptEvent(
                    text=frame.text,
                    is_final=True,
                    language=str(frame.language) if frame.language else None,
                )
            if event is not None:
                try:
                    await self._on_transcript(event)
                except Exception as e:
                    logger.warning(
                        "transcript callback failed ({}): {}", type(e).__name__, e
                    )

        await self.push_frame(frame, direction)


class StreamingTranscriber:
    """Feeds raw PCM16 mono audio to a streaming STT service.

    Usage: ``await start()``, then ``await feed(chunk)`` for each audio chunk;
    transcripts arrive on ``on_transcript`` as the provider emits them.
    ``await stop()`` flushes and tears down (safe to call more than once).
    """

    def __init__(
        self,
        stt_service,
        *,
        sample_rate: int,
        on_transcript: TranscriptCallback,
    ):
        self._sample_rate = sample_rate
        pipeline = Pipeline([stt_service, _TranscriptEmitter(on_transcript)])
        # cancel_on_idle_timeout=False: the default idle watchdog waits for
        # bot/user speaking frames that never occur in this two-node pipeline
        # and would cancel a healthy stream mid-way.
        self._task = PipelineTask(
            pipeline,
            params=PipelineParams(audio_in_sample_rate=sample_rate),
            cancel_on_idle_timeout=False,
        )
        self._runner = PipelineRunner(handle_sigint=False, force_gc=True)
        self._run_future: Optional[asyncio.Task] = None
        self._failed = asyncio.Event()
        self._failure: Optional[str] = None

        @self._task.event_handler("on_pipeline_error")
        async def _on_pipeline_error(task, frame):
            self._failure = getattr(frame, "error", None) or "pipeline error"
            self._failed.set()
            logger.warning("streaming STT pipeline error: {}", self._failure)

    @property
    def failed(self) -> bool:
        """True once the pipeline reported an error (e.g. provider died)."""
        return self._failed.is_set()

    async def start(self) -> None:
        """Start the pipeline and fail fast on provider connect/auth errors.

        Waits up to ``_STARTUP_GRACE_SECONDS`` for an early failure — a
        pipeline ``ErrorFrame`` or the runner task finishing — and raises
        ``RuntimeError`` so callers can reject the stream cleanly *before*
        telling the client it is ready.
        """
        self._run_future = asyncio.create_task(self._runner.run(self._task))
        failure_wait = asyncio.create_task(self._failed.wait())
        try:
            await asyncio.wait(
                {self._run_future, failure_wait},
                timeout=_STARTUP_GRACE_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not failure_wait.done():
                failure_wait.cancel()

        if not self._failed.is_set() and not self._run_future.done():
            return  # healthy: pipeline is running and reported no error

        message = self._failure or ""
        if self._run_future.done():
            exc = self._run_future.exception()
            if exc is not None:
                message = message or f"{type(exc).__name__}: {exc}"
            message = message or "pipeline ended during startup"
        else:
            await self._task.cancel()
            try:
                await self._run_future
            except Exception:
                pass
        self._run_future = None
        raise RuntimeError(f"streaming STT failed to start: {message or 'unknown'}")

    async def feed(self, audio: bytes) -> None:
        """Queue one chunk of raw PCM16 mono audio for transcription."""
        if not audio:
            return
        await self._task.queue_frame(
            InputAudioRawFrame(
                audio=audio,
                sample_rate=self._sample_rate,
                num_channels=1,
            )
        )

    async def stop(self) -> None:
        """Flush pending audio, wait for final transcripts, tear down.

        Deliberately swallows teardown errors (logging them with type): this
        runs on the WebSocket close path, which must never raise — a wedged
        provider teardown should not prevent the client socket from closing.
        """
        if self._run_future is None:
            return
        run_future, self._run_future = self._run_future, None
        try:
            await self._task.stop_when_done()
            await asyncio.wait_for(run_future, timeout=_STOP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("streaming STT did not stop gracefully; cancelling")
            await self._task.cancel()
            try:
                await run_future
            except Exception:
                pass
        except Exception as e:
            logger.warning("streaming STT stop error ({}): {}", type(e).__name__, e)

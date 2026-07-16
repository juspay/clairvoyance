"""Bounds mic→STT latency for the in-process SmallWebRTC transport.

The bot shares its event loop with aiortc. During each bot turn (LLM + TTS +
mixer + Opus encode) the input-side tasks get starved while mic audio keeps
arriving. The audio path has several UNBOUNDED queues in series and no stage
ever drops a frame, so every starved second becomes PERMANENT delay (measured
growing ~+10s per bot turn — stale audio reaches Soniox seconds after it was
spoken). pipecat's only recovery (``SmallWebRTCTrack._idle_watcher`` →
``discard_old_frames()``) fires only when the reader stops for 2s, never when
it merely lags (pipecat issue #3066 tracks the unbounded design).

Queues on the path (all unbounded):
  1. aiortc ``RemoteStreamTrack._queue``   — decoded frames awaiting the reader
  2. ``BaseInputTransport._audio_in_queue`` — audio-only, awaiting the handler
  3. each ``FrameProcessor.__input_queue``  — mixed frames (audio + system)

This governor drains queues 1 and 2 down to a small tail whenever their
backlog exceeds ~0.5s: both carry ONLY raw mic audio, and stale mic audio
stuck behind the bot's own turn is worthless in a live conversation. Queue 3
carries system frames too, so it is only MEASURED, never touched. A periodic
"WEBRTC-DIAG" line logs queue depths and event-loop lag so any remaining
accumulation point shows up in the logs.

The private attrs used (``_track_map``, ``_queue``, ``_audio_in_queue``,
``_FrameProcessor__input_queue``) are stable in the pinned pipecat/aiortc;
everything is accessed read-only except the two audio-only queue drains.
"""

import asyncio
import time
from typing import Any, List, Optional

import numpy as np
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import FilterControlFrame
from pipecat.transports.smallwebrtc.connection import (
    AUDIO_TRANSCEIVER_INDEX,
    SmallWebRTCConnection,
)

from app.core.logger import logger

# aiortc decodes inbound Opus into ~20ms frames, so 25 frames ~= 0.5s. The
# transport re-chunks to the same order of magnitude, so one threshold fits
# both audio queues.
_MAX_BACKLOG_FRAMES = 25
# Drain down to a small tail instead of empty so consumers always have the
# freshest audio without ever observing a starved queue.
_KEEP_FRAMES = 5
_CHECK_INTERVAL_SECS = 0.25
_DIAG_EVERY_TICKS = 8  # one WEBRTC-DIAG line every ~2s


class SpeechOnsetLogger(BaseAudioFilter):
    """Diagnostic passthrough filter: logs when speech energy arrives.

    Timestamps the moment the user's voice PHYSICALLY reaches the backend
    (independent of every queue and of Soniox), so mic→backend delay can be
    separated from backend→transcription delay in the logs. Audio passes
    through untouched.
    """

    # int16 RMS above this for _ONSET_FRAMES consecutive frames = speech.
    _RMS_THRESHOLD = 400
    _ONSET_FRAMES = 3
    # ~0.5s of quiet (20ms frames) ends the speech segment.
    _OFFSET_FRAMES = 25

    def __init__(self) -> None:
        self._loud_streak = 0
        self._quiet_streak = 0
        self._in_speech = False

    async def start(self, sample_rate: int):
        logger.info(f"WEBRTC-DIAG speech onset logger active (rate={sample_rate})")

    async def stop(self):
        pass

    async def process_frame(self, frame: FilterControlFrame):
        pass

    async def filter(self, audio: bytes) -> bytes:
        if audio:
            samples = np.frombuffer(audio, dtype=np.int16)
            rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
            if rms >= self._RMS_THRESHOLD:
                self._loud_streak += 1
                self._quiet_streak = 0
                if not self._in_speech and self._loud_streak >= self._ONSET_FRAMES:
                    self._in_speech = True
                    logger.info(f"WEBRTC-DIAG speech energy ONSET (rms={rms:.0f})")
            else:
                self._quiet_streak += 1
                self._loud_streak = 0
                if self._in_speech and self._quiet_streak >= self._OFFSET_FRAMES:
                    self._in_speech = False
                    logger.info("WEBRTC-DIAG speech energy ENDED")
        return audio


def _find_aiortc_queue(connection: SmallWebRTCConnection) -> Optional[asyncio.Queue]:
    # Read-only lookup: wait for the TRANSPORT to create the track wrapper.
    # Never call connection.audio_input_track() here — creating the wrapper
    # ourselves has side effects (disables the receiver, and can cache a
    # wrapper with _track=None if the remote track hasn't attached yet, which
    # would break the transport's audio reader).
    track = connection._track_map.get(AUDIO_TRANSCEIVER_INDEX)
    if track is None:
        return None
    queue = getattr(getattr(track, "_track", None), "_queue", None)
    return queue if isinstance(queue, asyncio.Queue) else None


def _find_audio_in_queue(agent: Any) -> Optional[asyncio.Queue]:
    # BaseInputTransport._audio_in_queue: exists once the pipeline has started.
    input_transport = getattr(getattr(agent, "transport", None), "_input", None)
    queue = getattr(input_transport, "_audio_in_queue", None)
    return queue if isinstance(queue, asyncio.Queue) else None


def _walk_processors(processor: Any, found: List[Any]) -> None:
    found.append(processor)
    for child in getattr(processor, "_processors", []) or []:
        _walk_processors(child, found)


def _find_stt_queue(agent: Any) -> Optional[asyncio.Queue]:
    pipeline = getattr(getattr(agent, "task", None), "_pipeline", None)
    if pipeline is None:
        return None
    processors: List[Any] = []
    _walk_processors(pipeline, processors)
    for proc in processors:
        if "STT" in type(proc).__name__:
            queue = getattr(proc, "_FrameProcessor__input_queue", None)
            return queue if isinstance(queue, asyncio.Queue) else None
    return None


def _drain(queue: asyncio.Queue) -> int:
    """Drop queued items down to a small tail; returns how many were dropped.

    Only used on the two audio-only queues. No awaits, so the drain is atomic
    on the event loop — consumers can't interleave mid-drain.
    """
    dropped = 0
    while queue.qsize() > _KEEP_FRAMES:
        item = queue.get_nowait()
        queue.task_done()
        if item is None:
            # aiortc's end-of-stream sentinel — put it back so the reader
            # still sees the track end.
            queue.put_nowait(None)
            break
        dropped += 1
    return dropped


async def run_input_latency_governor(
    connection: SmallWebRTCConnection, agent: Any
) -> None:
    """Run until cancelled; the owning bot cancels this task at teardown."""
    aiortc_q: Optional[asyncio.Queue] = None
    audio_in_q: Optional[asyncio.Queue] = None
    stt_q: Optional[asyncio.Queue] = None

    logger.info("SmallWebRTC input latency governor started")
    ticks = 0
    max_lag_ms = 0.0
    while True:
        before = time.monotonic()
        await asyncio.sleep(_CHECK_INTERVAL_SECS)
        # How late the sleep woke up = how starved the event loop is. The
        # input tasks share this loop, so their audio falls behind by
        # roughly this much during each stall.
        lag_ms = (time.monotonic() - before - _CHECK_INTERVAL_SECS) * 1000
        max_lag_ms = max(max_lag_ms, lag_ms)
        ticks += 1

        # Self-terminate when the peer connection dies. The owning bot cancels
        # this task in its finally-block, but a wedged pipeline teardown can
        # keep the bot task alive indefinitely — without this check the
        # governor would heartbeat forever after the call ended.
        pc_state = getattr(getattr(connection, "_pc", None), "connectionState", None)
        if pc_state in ("closed", "failed"):
            logger.info(
                "SmallWebRTC input latency governor exiting "
                f"(peer connection {pc_state})"
            )
            return

        # Late-bound lookups: these objects appear as transport/pipeline
        # setup progresses.
        aiortc_q = aiortc_q or _find_aiortc_queue(connection)
        audio_in_q = audio_in_q or _find_audio_in_queue(agent)
        stt_q = stt_q or _find_stt_queue(agent)

        for name, queue in (("aiortc", aiortc_q), ("audio_in", audio_in_q)):
            if queue is not None and queue.qsize() > _MAX_BACKLOG_FRAMES:
                dropped = _drain(queue)
                if dropped:
                    logger.warning(
                        f"SmallWebRTC {name} backlog: dropped {dropped} stale "
                        f"audio frames (~{dropped * 20}ms) to bound mic→STT latency"
                    )

        if ticks % _DIAG_EVERY_TICKS == 0:
            logger.info(
                "WEBRTC-DIAG "
                f"loop_lag_max={max_lag_ms:.0f}ms "
                f"aiortc_q={aiortc_q.qsize() if aiortc_q else 'n/a'} "
                f"audio_in_q={audio_in_q.qsize() if audio_in_q else 'n/a'} "
                f"stt_q={stt_q.qsize() if stt_q else 'n/a'}"
            )
            max_lag_ms = 0.0

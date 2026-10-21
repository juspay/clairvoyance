"""MeetingSession — one recording from "start" to "stop", kept alive for hours.

Story: the gate flips to capture and hands this session every transcript
line. The session numbers it, buffers it, and writes a batch every
``flush_seconds`` (or 50 lines, or on stop) as ONE insert. Alongside it runs
three small loops: a heartbeat on the meeting row (so a sweeper can tell a
live meeting from a dead bot), an STT rotation (a fresh provider session
before the provider's own limit, reusing the service's flush-on-disconnect),
and a hard duration cap. While recording, the user-idle handler is told to
stay quiet — composed with whatever predicate it already had (approval
cards), never overwriting it.

``stop()`` is idempotent: whichever door fires first (stop phrase, API, cap,
heartbeat finding the row already closed) wins, the rest are no-ops. The
meeting row's own ``WHERE state = 'RECORDING'`` guard is the last line of
defence; this class never bypasses it.

Everything the session touches from outside is injected (gate, idle handler,
STT service, emitter), so the whole lifecycle is unit-testable with fakes.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    List,
    Optional,
    Protocol,
    Sequence,
    Union,
    cast,
)

from app.ai.voice.agents.breeze_buddy.processors.transcription_gate import (
    CaptureLine,
)
from app.core.config.dynamic import (
    MEETING_HEARTBEAT_SECONDS,
    MEETING_MAX_DURATION_SECONDS,
    MEETING_SEGMENT_FLUSH_SECONDS,
    MEETING_STOP_MATCH_THRESHOLD,
    MEETING_STOP_PHRASE,
    MEETING_STT_ROTATE_SECONDS,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy import meetings as meetings_db
from app.schemas.breeze_buddy.meetings import (
    Meeting,
    MeetingEndReason,
    SegmentLine,
)

Emit = Callable[[str, Optional[dict]], Awaitable[None]]


class CaptureGate(Protocol):
    """The slice of TranscriptionGateProcessor the session drives."""

    def start_capture(
        self,
        *,
        on_line: Callable[[CaptureLine], Awaitable[None]],
        on_stop: Callable[[], Awaitable[None]],
        stop_phrase: Union[str, Sequence[str]],
        threshold: float = ...,
        on_interim: Optional[Callable[[str], Awaitable[None]]] = ...,
    ) -> None: ...

    def stop_capture(self) -> None: ...


OnFinished = Callable[[Meeting], Awaitable[None]]

#: Flush before the timer when this many lines are waiting.
FLUSH_AT_LINES = 50
#: Consecutive failed batch writes before the session gives up the meeting.
MAX_WRITE_FAILURES = 10


def split_stop_phrases(value: Union[str, Sequence[str], None]) -> List[str]:
    """``"a|b"`` or ``["a", "b"]`` → ``["a", "b"]``, blanks dropped."""
    if value is None:
        return []
    parts = value.split("|") if isinstance(value, str) else list(value)
    return [p.strip() for p in parts if p and p.strip()]


@dataclass(frozen=True)
class MeetingConfig:
    """Tunables, loaded from dynamic config once per recording."""

    stop_phrases: List[str]
    stop_threshold: float
    max_duration_seconds: int
    heartbeat_seconds: int
    flush_seconds: int
    stt_rotate_seconds: int

    @classmethod
    async def load(cls) -> "MeetingConfig":
        return cls(
            stop_phrases=split_stop_phrases(await MEETING_STOP_PHRASE()),
            stop_threshold=await MEETING_STOP_MATCH_THRESHOLD(),
            max_duration_seconds=await MEETING_MAX_DURATION_SECONDS(),
            heartbeat_seconds=await MEETING_HEARTBEAT_SECONDS(),
            flush_seconds=await MEETING_SEGMENT_FLUSH_SECONDS(),
            stt_rotate_seconds=await MEETING_STT_ROTATE_SECONDS(),
        )


@dataclass
class _Buffer:
    """Lines waiting for the next batch write, already numbered."""

    start_seq: int = 0
    lines: List[SegmentLine] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.lines)


class MeetingSession:
    """Owns one recording on one bot. Create one per ``start``."""

    def __init__(
        self,
        *,
        gate: CaptureGate,
        config: MeetingConfig,
        lead_id: str,
        merchant_id: str,
        reseller_id: str,
        stt_provider: str,
        idle_handler: Any = None,
        stt_service: Any = None,
        emit: Optional[Emit] = None,
        on_finished: Optional[OnFinished] = None,
    ) -> None:
        self._gate = gate
        self._config = config
        self._lead_id = lead_id
        self._merchant_id = merchant_id
        self._reseller_id = reseller_id
        self._stt_provider = stt_provider
        self._idle_handler = idle_handler
        self._stt_service = stt_service
        self._emit = emit
        self._on_finished = on_finished

        self.meeting: Optional[Meeting] = None
        self._recording = False
        self._stopping = False
        self._stop_lock = asyncio.Lock()
        self._started_monotonic = 0.0
        self._next_seq = 0
        self._buffer = _Buffer()
        self._write_failures = 0
        self._loops: List[asyncio.Task] = []
        self._previous_suppress: Optional[Callable[[], bool]] = None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def meeting_id(self) -> Optional[str]:
        return self.meeting.id if self.meeting else None

    @property
    def elapsed_seconds(self) -> int:
        if not self._recording:
            return 0
        return int(time.monotonic() - self._started_monotonic)

    @property
    def pending_lines(self) -> int:
        return len(self._buffer)

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    async def start(self) -> Meeting:
        """Open the meeting and flip the gate. Idempotent per session.

        If this lead already has a RECORDING meeting (a reconnected bot), it
        is reused and numbering continues after its last stored line.
        """
        if self._recording and self.meeting is not None:
            return self.meeting

        existing = await meetings_db.get_recording_meeting_by_lead(self._lead_id)
        if existing is not None:
            self.meeting = existing
            self._next_seq = await meetings_db.get_last_seq(existing.id) + 1
            logger.info(
                f"[MEETING] resuming {existing.id} on lead {self._lead_id} "
                f"from seq {self._next_seq}"
            )
        else:
            self.meeting = await meetings_db.create_meeting(
                meeting_id=f"mtg_{uuid.uuid4().hex[:20]}",
                reseller_id=self._reseller_id,
                merchant_id=self._merchant_id,
                lead_id=self._lead_id,
                stt_provider=self._stt_provider,
            )
            self._next_seq = 0
            logger.info(f"[MEETING] started {self.meeting.id} on lead {self._lead_id}")

        self._buffer = _Buffer(start_seq=self._next_seq)
        self._started_monotonic = time.monotonic()
        self._recording = True
        self._stopping = False

        self._suspend_idle()
        self._gate.start_capture(
            on_line=self.on_line,
            on_stop=self._on_stop_phrase,
            stop_phrase=self._config.stop_phrases,
            threshold=self._config.stop_threshold,
        )
        self._loops = [
            asyncio.create_task(self._flush_loop(), name="meeting-flush"),
            asyncio.create_task(self._heartbeat_loop(), name="meeting-heartbeat"),
            asyncio.create_task(self._cap_loop(), name="meeting-cap"),
        ]
        if self._stt_service is not None and self._config.stt_rotate_seconds > 0:
            self._loops.append(
                asyncio.create_task(self._rotate_loop(), name="meeting-stt-rotate")
            )

        await self._emit_event("recording_started", {"meeting_id": self.meeting.id})
        return self.meeting

    # ------------------------------------------------------------------
    # Lines
    # ------------------------------------------------------------------

    async def on_line(self, line: CaptureLine) -> None:
        """Gate callback: number the line and buffer it."""
        if not self._recording:
            return
        self._buffer.lines.append(
            SegmentLine(
                speaker=line.speaker,
                text=line.text,
                start_ms=line.start_ms,
                end_ms=max(line.end_ms, line.start_ms),
            )
        )
        self._next_seq += 1
        if len(self._buffer) >= FLUSH_AT_LINES:
            await self.flush()

    async def flush(self) -> int:
        """Write the buffered batch as one insert. Returns lines written.

        On failure the batch is kept for the next attempt (seq numbers are
        already assigned, so a retry is the same insert and therefore
        idempotent). After MAX_WRITE_FAILURES in a row the meeting is given
        up rather than silently losing hours of lines.
        """
        if not self._buffer.lines or self.meeting is None:
            return 0
        batch, self._buffer = self._buffer, _Buffer(start_seq=self._next_seq)
        try:
            written = await meetings_db.append_segments(
                self.meeting.id, batch.start_seq, batch.lines
            )
        except Exception as e:  # accessor already logged it
            # Put the batch back in front of anything buffered meanwhile.
            self._buffer = _Buffer(
                start_seq=batch.start_seq, lines=batch.lines + self._buffer.lines
            )
            self._write_failures += 1
            logger.warning(
                f"[MEETING] batch write failed ({self._write_failures}/"
                f"{MAX_WRITE_FAILURES}) for {self.meeting.id}: {e}"
            )
            if self._write_failures >= MAX_WRITE_FAILURES and self._recording:
                await self.stop(MeetingEndReason.STALE)
            return 0
        self._write_failures = 0
        return written

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    async def _on_stop_phrase(self) -> None:
        await self.stop(MeetingEndReason.VOICE)

    async def stop(self, reason: MeetingEndReason) -> Optional[Meeting]:
        """Close the recording once. Returns the finished meeting, or None if
        it had already been closed (by another door or by the sweeper)."""
        async with self._stop_lock:
            if not self._recording or self.meeting is None:
                return None
            self._stopping = True
            self._recording = False
            meeting_id = self.meeting.id

            self._gate.stop_capture()
            await self._cancel_loops()
            self._resume_idle()

            for _ in range(3):
                if not self._buffer.lines:
                    break
                await self._flush_batch_once()

            finished = await meetings_db.finish_meeting(meeting_id, reason)
            if finished is None:
                logger.info(
                    f"[MEETING] {meeting_id} was already closed ({reason.value})"
                )
                return None
            self.meeting = finished
            logger.info(
                f"[MEETING] stopped {meeting_id} after {finished.duration_s}s "
                f"({reason.value}), {self._next_seq} line(s)"
            )
            await self._emit_event(
                "recording_saved",
                {"meeting_id": meeting_id, "duration_s": finished.duration_s},
            )
            if self._on_finished is not None:
                try:
                    await self._on_finished(finished)
                except Exception as e:
                    logger.error(f"[MEETING] on_finished failed for {meeting_id}: {e}")
            return finished

    async def suspend(self) -> int:
        """The connection is going away but the meeting is not over.

        Stops the in-process loops and the gate, flushes what is buffered,
        restores the idle handler, and leaves the row RECORDING so a
        reconnect within the resume grace can continue it and the sweeper
        closes it otherwise. Returns lines flushed. Idempotent.
        """
        async with self._stop_lock:
            if not self._recording or self.meeting is None:
                return 0
            self._recording = False
            self._gate.stop_capture()
            await self._cancel_loops()
            self._resume_idle()
            pending = len(self._buffer)
            for _ in range(3):
                if not self._buffer.lines:
                    break
                await self._flush_batch_once()
            logger.info(
                f"[MEETING] suspended {self.meeting.id} with {self._next_seq} "
                f"line(s) numbered; row stays RECORDING for resume"
            )
            return pending - len(self._buffer)

    async def _flush_batch_once(self) -> None:
        """One final-flush attempt that never raises and never re-stops."""
        if not self._buffer.lines or self.meeting is None:
            return
        batch, self._buffer = self._buffer, _Buffer(start_seq=self._next_seq)
        try:
            await meetings_db.append_segments(
                self.meeting.id, batch.start_seq, batch.lines
            )
        except Exception:
            self._buffer = _Buffer(
                start_seq=batch.start_seq, lines=batch.lines + self._buffer.lines
            )

    # ------------------------------------------------------------------
    # Loops
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        interval = max(1, self._config.flush_seconds)
        while self._recording:
            await asyncio.sleep(interval)
            if self._recording:
                await self.flush()

    async def _heartbeat_loop(self) -> None:
        interval = max(1, self._config.heartbeat_seconds)
        while self._recording and self.meeting is not None:
            await asyncio.sleep(interval)
            if not self._recording:
                return
            try:
                alive = await meetings_db.heartbeat_meeting(self.meeting.id)
            except Exception:
                continue  # transient; the sweeper's grace covers a missed beat
            if not alive:
                # Someone else closed the row (sweeper, API). Fold locally.
                logger.warning(
                    f"[MEETING] {self.meeting.id} no longer RECORDING in DB; "
                    "stopping locally"
                )
                await self.stop(MeetingEndReason.STALE)
                return

    async def _cap_loop(self) -> None:
        await asyncio.sleep(max(1, self._config.max_duration_seconds))
        if self._recording:
            logger.info(
                f"[MEETING] {self.meeting_id} hit the "
                f"{self._config.max_duration_seconds}s cap"
            )
            await self.stop(MeetingEndReason.MAX_DURATION)

    async def _rotate_loop(self) -> None:
        interval = max(1, self._config.stt_rotate_seconds)
        while self._recording:
            await asyncio.sleep(interval)
            if self._recording:
                await self.rotate_stt()

    async def rotate_stt(self) -> bool:
        """Reconnect the STT service in place, before the provider's own
        session limit. The Soniox service flushes buffered finals on
        disconnect, so no words are lost; seq numbering is ours, so the
        transcript shows no seam."""
        svc = self._stt_service
        disconnect = getattr(svc, "_disconnect", None)
        connect = getattr(svc, "_connect", None)
        if not callable(disconnect) or not callable(connect):
            return False
        reconnect = cast(Callable[[], Awaitable[Any]], disconnect)
        connect_again = cast(Callable[[], Awaitable[Any]], connect)
        try:
            await reconnect()
            await connect_again()
            logger.info(f"[MEETING] rotated STT session for {self.meeting_id}")
            return True
        except Exception as e:
            logger.error(f"[MEETING] STT rotation failed for {self.meeting_id}: {e}")
            return False

    async def _cancel_loops(self) -> None:
        loops, self._loops = self._loops, []
        current = asyncio.current_task()
        for task in loops:
            if task is current or task.done():
                continue
            task.cancel()
        for task in loops:
            if task is current:
                continue
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Idle timers
    # ------------------------------------------------------------------

    def _suspend_idle(self) -> None:
        """Keep the user-idle handler quiet while recording, on top of any
        predicate it already has (an approval card pending)."""
        handler = self._idle_handler
        if handler is None:
            return
        previous = getattr(handler, "suppress_when", None)
        self._previous_suppress = previous

        def suppress() -> bool:
            if self._recording:
                return True
            return bool(previous()) if callable(previous) else False

        handler.suppress_when = suppress

    def _resume_idle(self) -> None:
        handler = self._idle_handler
        if handler is None:
            return
        handler.suppress_when = self._previous_suppress
        self._previous_suppress = None

    # ------------------------------------------------------------------
    # Device events
    # ------------------------------------------------------------------

    async def _emit_event(self, event: str, payload: Optional[dict]) -> None:
        if self._emit is None:
            return
        try:
            await self._emit(event, payload)
        except Exception as e:
            logger.warning(f"[MEETING] failed to emit {event}: {e}")

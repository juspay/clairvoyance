"""MeetingSession — one recording, held in memory for the length of the call.

Story: the LLM calls ``start_meeting_recording`` → ``start()`` flips
``is_recording``. From then on the mute strategy keeps speech away from the
LLM and the observer hands every final here as lines (``add_line``). Two
small loops run meanwhile: a hard duration cap and a silence cap (nobody
spoke for N minutes → the meeting is over). A dropped STT socket is not our
problem: pipecat's websocket service reconnects it on its own, and Soniox's
5-hour stream limit sits above the duration cap.
``stop()`` is idempotent across doors (stop phrase, caps, call end): it
freezes a ``MeetingRecord``, emits ``recording_ended`` plus the lines in
chunks to the client, and hands the record to ``on_finished`` (the handler
speaks the duration and runs the template's delivery hooks).

Nothing is persisted. A restart mid-meeting loses that meeting — by design,
the consumer of the events/hook owns storage.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, List, Optional

from app.ai.voice.agents.breeze_buddy.recorder.phrases import (
    is_stop_command,
    split_phrases,
)
from app.core.config.dynamic import (
    MEETING_LINE_MERGE_GAP_MS,
    MEETING_MAX_DURATION_SECONDS,
    MEETING_SILENCE_TIMEOUT_SECONDS,
    MEETING_STOP_MATCH_THRESHOLD,
    MEETING_STOP_PHRASE,
)
from app.core.logger import logger

Emit = Callable[[str, Optional[dict]], Awaitable[None]]

#: Lines per ``recording_lines`` event; small enough for Daily app messages.
LINES_PER_EVENT = 25

END_VOICE = "voice"
END_MAX_DURATION = "max_duration"
END_SILENCE = "silence"
END_CALL_ENDED = "call_ended"


@dataclass(frozen=True)
class MeetingConfig:
    """Tunables, loaded from dynamic config once per call."""

    stop_phrases: List[str]
    stop_threshold: float
    max_duration_seconds: int
    silence_timeout_seconds: int
    merge_gap_ms: int

    @classmethod
    async def load(cls) -> "MeetingConfig":
        return cls(
            stop_phrases=split_phrases(await MEETING_STOP_PHRASE()),
            stop_threshold=await MEETING_STOP_MATCH_THRESHOLD(),
            max_duration_seconds=await MEETING_MAX_DURATION_SECONDS(),
            silence_timeout_seconds=await MEETING_SILENCE_TIMEOUT_SECONDS(),
            merge_gap_ms=await MEETING_LINE_MERGE_GAP_MS(),
        )


@dataclass
class MeetingLine:
    """One transcript line; offsets are ms from recording start."""

    speaker: Optional[str]
    text: str
    start_ms: int
    end_ms: int

    def as_dict(self) -> dict:
        return {
            "speaker": self.speaker,
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
        }


@dataclass(frozen=True)
class MeetingRecord:
    """The frozen result of one recording, handed to on_finished and hooks."""

    meeting_id: str
    lead_id: str
    merchant_id: str
    reseller_id: str
    started_at: datetime
    ended_at: datetime
    duration_s: int
    end_reason: str
    lines: List[MeetingLine] = field(default_factory=list)


class MeetingSession:
    """Owns one recording on one bot. Created at call start when the template
    enables meeting recording; ``start()`` is what the LLM's function flips."""

    def __init__(
        self,
        *,
        config: MeetingConfig,
        lead_id: str,
        merchant_id: str,
        reseller_id: str,
        idle_handler: Any = None,
        emit: Optional[Emit] = None,
        on_finished: Optional[Callable[[MeetingRecord], Awaitable[None]]] = None,
    ) -> None:
        self.config = config
        self.lead_id = lead_id
        self.merchant_id = merchant_id
        self.reseller_id = reseller_id
        self.idle_handler = idle_handler
        self.emit = emit
        self.on_finished = on_finished

        self.meeting_id: Optional[str] = None
        self.lines: List[MeetingLine] = []
        self.record: Optional[MeetingRecord] = None
        self._recording = False
        self._started_at: Optional[datetime] = None
        self._started_monotonic = 0.0
        self._last_activity_monotonic = 0.0
        self._token_origin_ms: Optional[int] = None
        self._stop_lock = asyncio.Lock()
        # The transcription frame that carried the stop phrase: it ended the
        # recording, so the mute strategy must still swallow it downstream.
        self.stop_frame_id: Optional[int] = None
        self._loops: List[asyncio.Task] = []
        self._previous_suppress: Optional[Callable[[], bool]] = None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def elapsed_seconds(self) -> int:
        if not self._recording:
            return 0
        return int(time.monotonic() - self._started_monotonic)

    def elapsed_ms(self) -> int:
        if not self._recording:
            return 0
        return int((time.monotonic() - self._started_monotonic) * 1000)

    def is_stop_command(self, text: str) -> bool:
        return is_stop_command(
            text, self.config.stop_phrases, self.config.stop_threshold
        )

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    async def start(self) -> str:
        """Begin recording. Idempotent: a second call returns the same id."""
        if self._recording and self.meeting_id:
            return self.meeting_id
        self.meeting_id = f"mtg_{uuid.uuid4().hex[:20]}"
        self.lines = []
        self.record = None
        self._started_at = datetime.now(timezone.utc)
        self._started_monotonic = time.monotonic()
        self._last_activity_monotonic = self._started_monotonic
        self._token_origin_ms = None
        self._recording = True
        self.stop_frame_id = None

        self._suspend_idle()
        self._loops = [
            asyncio.create_task(self._cap_loop(), name="meeting-cap"),
            asyncio.create_task(self._silence_loop(), name="meeting-silence"),
        ]
        logger.info(f"[MEETING] started {self.meeting_id} on lead {self.lead_id}")
        await self._emit_event(
            "recording_started",
            {"meeting_id": self.meeting_id, "started_at": self._started_at.isoformat()},
        )
        return self.meeting_id

    # ------------------------------------------------------------------
    # Lines
    # ------------------------------------------------------------------

    def offset_ms(self, token_ms: Any) -> int:
        """Token time → ms from recording start; wall clock when absent."""
        if not isinstance(token_ms, (int, float)):
            return self.elapsed_ms()
        if self._token_origin_ms is None:
            self._token_origin_ms = int(token_ms)
        return max(0, int(token_ms) - self._token_origin_ms)

    def add_line(
        self, speaker: Optional[str], text: str, start_ms: int, end_ms: int
    ) -> None:
        """Append a line, merging into the previous one when the same speaker
        continues within ``merge_gap_ms`` (STT emits short fragments)."""
        if not self._recording:
            return
        text = text.strip()
        if not text:
            return
        self._last_activity_monotonic = time.monotonic()
        end_ms = max(end_ms, start_ms)
        last = self.lines[-1] if self.lines else None
        if (
            last is not None
            and last.speaker == speaker
            and 0 <= start_ms - last.end_ms <= self.config.merge_gap_ms
        ):
            last.text = f"{last.text} {text}"
            last.end_ms = max(last.end_ms, end_ms)
            return
        self.lines.append(
            MeetingLine(speaker=speaker, text=text, start_ms=start_ms, end_ms=end_ms)
        )

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    async def stop(
        self, reason: str, *, stop_frame_id: Optional[int] = None
    ) -> Optional[MeetingRecord]:
        """End the recording once. Returns the record, or None if already ended."""
        async with self._stop_lock:
            if (
                not self._recording
                or self.meeting_id is None
                or self._started_at is None
            ):
                return None
            self.stop_frame_id = stop_frame_id
            self._recording = False
            await self._cancel_loops()
            self._resume_idle()

            ended_at = datetime.now(timezone.utc)
            duration = max(0, int((ended_at - self._started_at).total_seconds()))
            record = MeetingRecord(
                meeting_id=self.meeting_id,
                lead_id=self.lead_id,
                merchant_id=self.merchant_id,
                reseller_id=self.reseller_id,
                started_at=self._started_at,
                ended_at=ended_at,
                duration_s=duration,
                end_reason=reason,
                lines=list(self.lines),
            )
            self.record = record
            logger.info(
                f"[MEETING] ended {record.meeting_id} after {duration}s "
                f"({reason}), {len(record.lines)} line(s)"
            )
            await self._emit_event(
                "recording_ended",
                {
                    "meeting_id": record.meeting_id,
                    "duration_s": duration,
                    "end_reason": reason,
                    "line_count": len(record.lines),
                    "started_at": record.started_at.isoformat(),
                    "ended_at": record.ended_at.isoformat(),
                },
            )
            await self._emit_lines(record)
            if self.on_finished is not None:
                try:
                    await self.on_finished(record)
                except Exception as e:
                    logger.error(
                        f"[MEETING] on_finished failed for {record.meeting_id}: {e}"
                    )
            return record

    async def _emit_lines(self, record: MeetingRecord) -> None:
        lines = [line.as_dict() for line in record.lines]
        if not lines:
            return
        chunks = [
            lines[i : i + LINES_PER_EVENT]
            for i in range(0, len(lines), LINES_PER_EVENT)
        ]
        for index, chunk in enumerate(chunks):
            await self._emit_event(
                "recording_lines",
                {
                    "meeting_id": record.meeting_id,
                    "chunk": index + 1,
                    "chunks": len(chunks),
                    "lines": chunk,
                },
            )

    # ------------------------------------------------------------------
    # Loops
    # ------------------------------------------------------------------

    async def _cap_loop(self) -> None:
        await asyncio.sleep(max(1, self.config.max_duration_seconds))
        if self._recording:
            logger.info(f"[MEETING] {self.meeting_id} hit the duration cap")
            await self.stop(END_MAX_DURATION)

    async def _silence_loop(self) -> None:
        timeout = self.config.silence_timeout_seconds
        if timeout <= 0:
            return
        while self._recording:
            await asyncio.sleep(min(30, max(1, timeout)))
            if not self._recording:
                return
            if time.monotonic() - self._last_activity_monotonic >= timeout:
                logger.info(f"[MEETING] {self.meeting_id}: {timeout}s of silence")
                await self.stop(END_SILENCE)
                return

    async def _cancel_loops(self) -> None:
        loops, self._loops = self._loops, []
        current = asyncio.current_task()
        for task in loops:
            if task is not current and not task.done():
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
        handler = self.idle_handler
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
        handler = self.idle_handler
        if handler is None:
            return
        handler.suppress_when = self._previous_suppress
        self._previous_suppress = None

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def _emit_event(self, event: str, payload: Optional[dict]) -> None:
        if self.emit is None:
            return
        try:
            await self.emit(event, payload)
        except Exception as e:
            logger.warning(f"[MEETING] failed to emit {event}: {e}")

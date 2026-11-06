"""Collect transcript lines and hear the stop phrase, from outside the pipeline.

A pipecat observer sees every frame pushed between processors without being
a processor. While the session records, each final ``TranscriptionFrame`` is
either the stop command (→ ``session.stop``) or transcript lines, split by
the STT provider's speaker labels (Soniox tokens on ``frame.result``).
A frame is observed once per link, so it is de-duplicated by id.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Deque, List, Optional, Set, Tuple

from pipecat.frames.frames import TranscriptionFrame
from pipecat.observers.base_observer import BaseObserver, FramePushed

from app.ai.voice.agents.breeze_buddy.recorder.session import (
    END_VOICE,
    MeetingSession,
)

_SEEN_LIMIT = 256


class MeetingRecordingObserver(BaseObserver):
    """Feeds finals to the session while it records; inert otherwise."""

    def __init__(self, session: MeetingSession, **kwargs):
        super().__init__(**kwargs)
        self._session = session
        self._seen: Set[int] = set()
        self._seen_order: Deque[int] = deque()

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if not isinstance(frame, TranscriptionFrame):
            return
        if not self._session.is_recording:
            return
        if self._already_seen(frame.id):
            return
        if self._session.is_stop_command(frame.text):
            await self._session.stop(END_VOICE, stop_frame_id=frame.id)
            return
        for speaker, text, start_ms, end_ms in self._split(frame):
            self._session.add_line(speaker, text, start_ms, end_ms)

    def _already_seen(self, frame_id: int) -> bool:
        if frame_id in self._seen:
            return True
        self._seen.add(frame_id)
        self._seen_order.append(frame_id)
        if len(self._seen_order) > _SEEN_LIMIT:
            self._seen.discard(self._seen_order.popleft())
        return False

    def _split(
        self, frame: TranscriptionFrame
    ) -> List[Tuple[Optional[str], str, int, int]]:
        """One (speaker, text, start, end) per consecutive speaker run."""
        tokens = frame.result if isinstance(frame.result, list) else None
        if not tokens or not all(isinstance(t, dict) for t in tokens):
            text = frame.text.strip()
            if not text:
                return []
            now = self._session.elapsed_ms()
            return [(None, text, now, now)]

        out: List[Tuple[Optional[str], str, int, int]] = []
        run: List[dict] = []
        run_speaker: Any = None

        def flush() -> None:
            if not run:
                return
            text = "".join(str(t.get("text", "")) for t in run).strip()
            if text:
                out.append(
                    (
                        str(run_speaker) if run_speaker is not None else None,
                        text,
                        self._session.offset_ms(run[0].get("start_ms")),
                        self._session.offset_ms(run[-1].get("end_ms")),
                    )
                )
            run.clear()

        for token in tokens:
            speaker = token.get("speaker")
            if run and speaker != run_speaker:
                flush()
            if not run:
                run_speaker = speaker
            run.append(token)
        flush()
        return out

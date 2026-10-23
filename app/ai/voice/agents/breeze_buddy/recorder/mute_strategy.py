"""Keep speech away from the LLM while a meeting is being recorded.

A pipecat user-mute strategy on the aggregator: when it returns True the
aggregator drops the user's frames before the LLM — the same mechanism the
``disabled_discard`` interruption mode uses. The RTVI observer sits upstream,
so the client still receives live ``user-transcription`` messages.
"""

from pipecat.frames.frames import Frame
from pipecat.turns.user_mute import BaseUserMuteStrategy

from app.ai.voice.agents.breeze_buddy.recorder.session import MeetingSession


class MeetingRecordingMuteStrategy(BaseUserMuteStrategy):
    """Mute the user for the LLM exactly while ``session.is_recording``."""

    def __init__(self, session: MeetingSession, **kwargs):
        super().__init__(**kwargs)
        self._session = session

    async def process_frame(self, frame: Frame) -> bool:
        if self._session.is_recording:
            return True
        # The frame that carried the stop phrase ended the recording a moment
        # ago (the observer runs first); it is a command, not a turn for the LLM.
        stop_id = self._session.stop_frame_id
        return stop_id is not None and getattr(frame, "id", None) == stop_id

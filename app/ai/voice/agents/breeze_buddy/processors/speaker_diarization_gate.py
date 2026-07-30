"""Primary-speaker filtering for Soniox diarized transcripts."""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any, Optional

from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.ai.voice.agents.breeze_buddy.template.types import (
    SonioxSpeakerFilterConfig,
    UnknownSpeakerPolicy,
)
from app.core.logger import logger


class SpeakerDiarizationGateProcessor(FrameProcessor):
    """Lock to the first qualifying Soniox speaker and drop all others.

    Soniox annotates individual tokens in ``frame.result`` with a session-local
    ``speaker`` value. Before a primary speaker is locked, interim frames are
    held back. The first finalized frame with enough words selects its dominant
    speaker using token duration (falling back to token text length). From then
    on, both interim and final frames are rebuilt from that speaker's tokens.

    Soniox speaker IDs are local to a websocket connection. Call ``reset``
    whenever Soniox establishes a new connection so the next finalized frame
    can select that connection's primary speaker.
    """

    def __init__(self, config: SonioxSpeakerFilterConfig, **kwargs):
        super().__init__(**kwargs)
        self._config = config
        self._primary_speaker_id: Optional[str] = None
        self._dropped_frames = 0
        self._passed_frames = 0

    @property
    def primary_speaker_id(self) -> Optional[str]:
        return self._primary_speaker_id

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    def reset(self) -> None:
        """Clear the connection-local primary-speaker lock."""
        previous_speaker_id = self._primary_speaker_id
        self._primary_speaker_id = None
        if previous_speaker_id is not None:
            logger.info(
                "SpeakerDiarizationGate: reset primary speaker={} for new Soniox "
                "connection",
                previous_speaker_id,
            )

    @staticmethod
    def _speaker_id(token: dict[str, Any]) -> Optional[str]:
        speaker = token.get("speaker")
        return str(speaker) if speaker is not None else None

    @staticmethod
    def _token_score(token: dict[str, Any]) -> float:
        start_ms = token.get("start_ms")
        end_ms = token.get("end_ms")
        if isinstance(start_ms, (int, float)) and isinstance(end_ms, (int, float)):
            return max(float(end_ms) - float(start_ms), 1.0)
        return float(max(len(str(token.get("text", "")).strip()), 1))

    @staticmethod
    def _word_count(text: str) -> int:
        return len(text.split())

    @staticmethod
    def _tokens(
        frame: TranscriptionFrame | InterimTranscriptionFrame,
    ) -> Optional[list[dict[str, Any]]]:
        if not isinstance(frame.result, list):
            return None
        return [token for token in frame.result if isinstance(token, dict)]

    def _dominant_speaker(self, tokens: list[dict[str, Any]]) -> Optional[str]:
        scores: dict[str, float] = defaultdict(float)
        for token in tokens:
            if speaker := self._speaker_id(token):
                scores[speaker] += self._token_score(token)
        return max(scores, key=lambda speaker: scores[speaker]) if scores else None

    def _handle_missing_metadata(
        self, frame: TranscriptionFrame | InterimTranscriptionFrame
    ) -> Optional[Frame]:
        if self._config.unknown_speaker_policy == UnknownSpeakerPolicy.PASS:
            self._passed_frames += 1
            logger.debug(
                "SpeakerDiarizationGate: passing transcription without speaker metadata"
            )
            return frame
        self._dropped_frames += 1
        logger.debug(
            "SpeakerDiarizationGate: dropping transcription without speaker metadata"
        )
        return None

    def _filter_transcription(
        self, frame: TranscriptionFrame | InterimTranscriptionFrame
    ) -> Optional[Frame]:
        tokens = self._tokens(frame)
        if not tokens or not any(self._speaker_id(token) for token in tokens):
            return self._handle_missing_metadata(frame)

        if self._primary_speaker_id is None:
            if isinstance(frame, InterimTranscriptionFrame):
                self._dropped_frames += 1
                return None

            candidate = self._dominant_speaker(tokens)
            if candidate is None:
                return self._handle_missing_metadata(frame)

            candidate_text = "".join(
                str(token.get("text", ""))
                for token in tokens
                if self._speaker_id(token) == candidate
            )
            if self._word_count(candidate_text) < self._config.min_words:
                self._dropped_frames += 1
                logger.debug(
                    "SpeakerDiarizationGate: waiting for qualifying primary speaker "
                    "transcript (words={}, required={})",
                    self._word_count(candidate_text),
                    self._config.min_words,
                )
                return None

            self._primary_speaker_id = candidate
            logger.info(
                "SpeakerDiarizationGate: locked primary speaker={} from {} detected "
                "speaker(s)",
                candidate,
                len(
                    {
                        self._speaker_id(token)
                        for token in tokens
                        if self._speaker_id(token)
                    }
                ),
            )

        filtered_tokens = [
            token
            for token in tokens
            if self._speaker_id(token) == self._primary_speaker_id
            or (
                self._speaker_id(token) is None
                and self._config.unknown_speaker_policy == UnknownSpeakerPolicy.PASS
            )
        ]
        filtered_text = "".join(str(token.get("text", "")) for token in filtered_tokens)
        if not filtered_text.strip():
            self._dropped_frames += 1
            logger.debug(
                "SpeakerDiarizationGate: dropped non-primary speaker transcription"
            )
            return None

        filtered_frame = copy.copy(frame)
        filtered_frame.text = filtered_text
        filtered_frame.result = filtered_tokens
        self._passed_frames += 1
        return filtered_frame

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if direction == FrameDirection.DOWNSTREAM and isinstance(
            frame, (TranscriptionFrame, InterimTranscriptionFrame)
        ):
            filtered = self._filter_transcription(frame)
            if filtered is None:
                return
            frame = filtered

        await self.push_frame(frame, direction)

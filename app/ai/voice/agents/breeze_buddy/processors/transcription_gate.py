"""
Transcription Gate Processor

A single processor that controls which TranscriptionFrames reach the user
aggregator (and therefore the LLM). It provides two independent, composable
suppression modes:

1. Hard mute (VAD-independent)
   Activated via mute() / unmute() — typically called from a node pre-action
   when VAD is disabled. Drops ALL TranscriptionFrames unconditionally while
   muted, regardless of bot activity state.

2. Keyword filter (bot-active-aware)
   Configured at template level via KeywordFilterConfig. Drops TranscriptionFrames
   whose text matches configured keywords, but only while the bot is actively
   processing (LLM) or speaking (TTS). When the bot is idle, keyword-matching
   transcriptions pass through normally.

3. Capture (meeting recording)
   Activated via start_capture() / stop_capture() by the recorder session.
   While capturing, every final TranscriptionFrame is split into lines by
   speaker and handed to ``on_line``, then dropped — nothing reaches the
   LLM. Each final is first compared, as a WHOLE normalised sentence, with
   the stop phrase; a match calls ``on_stop`` instead. Interims are
   dropped (optionally reported via ``on_interim`` for a device display).
   The processor stays IO-free: it only calls the callbacks it was given.

All modes compose — any active condition causes a drop. Precedence while
capturing: hard mute → capture → keyword filter.

Pipeline position:
    transport.input()
    → stt
    → TranscriptionGateProcessor   ← here
    → user_aggregator (VAD / turn strategies)
    → llm
    ...

This processor is always inserted in the pipeline. When neither mode is active
it is a transparent passthrough with negligible overhead.
"""

import asyncio
import difflib
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional, Sequence, Union

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.ai.voice.agents.breeze_buddy.template.types import (
    KeywordFilterConfig,
    KeywordMatchType,
)
from app.core.logger import logger


@dataclass(frozen=True)
class CaptureLine:
    """One transcript line produced in capture mode.

    ``start_ms``/``end_ms`` are offsets from the moment capture started —
    from the STT token timings when the provider supplies them, else from the
    wall clock. ``speaker`` is the provider's diarization label or None.
    """

    speaker: Optional[str]
    text: str
    start_ms: int
    end_ms: int


OnLine = Callable[[CaptureLine], Awaitable[None]]
OnStop = Callable[[], Awaitable[None]]
OnInterim = Callable[[str], Awaitable[None]]


class TranscriptionGateProcessor(FrameProcessor):
    """Controls which TranscriptionFrames reach the LLM via two independent modes:
    hard mute and keyword filtering. Both modes drop frames silently — no frame
    is ever pushed downstream when either suppression condition is met.
    """

    def __init__(
        self,
        keyword_filter_config: Optional[KeywordFilterConfig] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)

        # --- Hard mute state ---
        self._muted: bool = False
        self._timed_unmute_task: Optional[asyncio.Task] = None

        # --- Keyword filter state ---
        if keyword_filter_config and keyword_filter_config.enabled:
            self._keyword_filter_enabled = True
            self._match_type = keyword_filter_config.match_type
            self._keywords: list[str] = [
                norm
                for kw in keyword_filter_config.keywords
                if (norm := self._normalise(kw))
            ]
        else:
            self._keyword_filter_enabled = False
            self._match_type = KeywordMatchType.EXACT
            self._keywords = []

        # --- Capture (meeting recording) state ---
        self._capturing: bool = False
        self._on_line: Optional[OnLine] = None
        self._on_stop: Optional[OnStop] = None
        self._on_interim: Optional[OnInterim] = None
        self._stop_phrases: List[str] = []
        self._stop_threshold: float = 1.0
        self._capture_started_monotonic: float = 0.0
        # First token start seen after capture began: origin of token-time
        # offsets, so a provider clock that started minutes earlier is irrelevant.
        self._capture_token_origin_ms: Optional[int] = None

        # --- Bot activity tracking (for keyword filter) ---
        # Track LLM and TTS independently so the BOTH → LLM_PROCESSING transition
        # (TTS stops but LLM still running) keeps the filter active.
        self._llm_active: bool = False
        self._tts_active: bool = False

    # ------------------------------------------------------------------
    # Hard mute API
    # ------------------------------------------------------------------

    def mute(self) -> None:
        """Drop ALL TranscriptionFrames until unmute() is called."""
        self._cancel_timed_unmute()
        self._muted = True
        logger.info("TranscriptionGate: hard mute engaged")

    def unmute(self) -> None:
        """Resume normal passthrough (keyword filter still applies if configured)."""
        self._cancel_timed_unmute()
        self._muted = False
        logger.info("TranscriptionGate: hard mute released")

    def mute_for(self, duration: float) -> None:
        """Drop ALL TranscriptionFrames for *duration* seconds, then auto-unmute.

        Any previous timed-unmute task is cancelled before scheduling a new one.
        An explicit call to unmute() (or another mute/mute_for) also cancels the
        pending timer.
        """
        self._cancel_timed_unmute()
        self._muted = True
        self._timed_unmute_task = asyncio.create_task(self._auto_unmute(duration))
        logger.info(f"TranscriptionGate: hard mute engaged for {duration}s")

    def _cancel_timed_unmute(self) -> None:
        """Cancel any pending timed-unmute task."""
        if self._timed_unmute_task and not self._timed_unmute_task.done():
            self._timed_unmute_task.cancel()
            logger.debug("TranscriptionGate: cancelled pending timed unmute")
        self._timed_unmute_task = None

    async def _auto_unmute(self, duration: float) -> None:
        """Sleep for *duration* seconds then release the hard mute."""
        try:
            await asyncio.sleep(duration)
            self._muted = False
            self._timed_unmute_task = None
            logger.info(f"TranscriptionGate: hard mute auto-released after {duration}s")
        except asyncio.CancelledError:
            pass

    @property
    def is_muted(self) -> bool:
        return self._muted

    # ------------------------------------------------------------------
    # Capture (meeting recording) API
    # ------------------------------------------------------------------

    def start_capture(
        self,
        *,
        on_line: OnLine,
        on_stop: OnStop,
        stop_phrase: Union[str, Sequence[str]],
        threshold: float = 0.9,
        on_interim: Optional[OnInterim] = None,
    ) -> None:
        """Divert every final transcription to ``on_line`` until a stop phrase.

        ``stop_phrase`` is one phrase or several. Each final is compared as a
        whole normalised sentence with every phrase (difflib similarity
        ``>= threshold``); a longer sentence that merely contains one is a
        transcript line, not a command. Calling this while already capturing
        just replaces the callbacks.
        """
        phrases = [stop_phrase] if isinstance(stop_phrase, str) else list(stop_phrase)
        normalised = [self._normalise(p) for p in phrases if p and p.strip()]
        normalised = [p for p in normalised if p]
        if not normalised:
            raise ValueError("start_capture: stop_phrase must not be empty")
        if not 0.0 < threshold <= 1.0:
            raise ValueError("start_capture: threshold must be in (0, 1]")
        self._on_line = on_line
        self._on_stop = on_stop
        self._on_interim = on_interim
        self._stop_phrases = normalised
        self._stop_threshold = threshold
        self._capture_started_monotonic = time.monotonic()
        self._capture_token_origin_ms = None
        self._capturing = True
        logger.info("TranscriptionGate: capture engaged (meeting recording)")

    def stop_capture(self) -> None:
        """Back to pass-through. Safe to call when not capturing."""
        if not self._capturing:
            return
        self._capturing = False
        self._on_line = None
        self._on_stop = None
        self._on_interim = None
        logger.info("TranscriptionGate: capture released")

    @property
    def is_capturing(self) -> bool:
        return self._capturing

    def capture_elapsed_ms(self) -> int:
        """Milliseconds since capture started (0 when not capturing)."""
        if not self._capturing:
            return 0
        return int((time.monotonic() - self._capture_started_monotonic) * 1000)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_bot_active(self) -> bool:
        return self._llm_active or self._tts_active

    @staticmethod
    def _normalise(text: str) -> str:
        """Strip surrounding whitespace and remove punctuation/symbols, preserving
        Unicode letters, digits, and combining marks so multilingual keywords
        (Indic, Arabic, etc.) round-trip correctly without vowel-sign collisions."""
        cleaned = "".join(
            c
            for c in text.strip().lower()
            if unicodedata.category(c)[0] not in ("P", "S")
        )
        return cleaned.strip()

    def _keyword_matches(self, text: str) -> bool:
        """Return True if *text* matches any configured keyword."""
        normalised = self._normalise(text)
        if self._match_type == KeywordMatchType.EXACT:
            return normalised in self._keywords
        # INCLUDES
        return any(kw in normalised for kw in self._keywords)

    def _is_stop_command(self, text: str) -> bool:
        """Whole-sentence similarity against any configured stop phrase."""
        normalised = self._normalise(text)
        if not normalised:
            return False
        for phrase in self._stop_phrases:
            if normalised == phrase:
                return True
            if (
                difflib.SequenceMatcher(None, normalised, phrase).ratio()
                >= self._stop_threshold
            ):
                return True
        return False

    def _split_lines(self, frame: TranscriptionFrame) -> List[CaptureLine]:
        """Turn one final frame into lines, one per consecutive speaker run.

        Soniox puts the raw token list on ``frame.result`` (``text``, and
        with diarization ``speaker``, ``start_ms``, ``end_ms``). Any other
        shape — or a provider without tokens — yields one line from
        ``frame.text`` timed by the wall clock.
        """
        tokens = frame.result if isinstance(frame.result, list) else None
        if not tokens or not all(isinstance(t, dict) for t in tokens):
            text = frame.text.strip()
            if not text:
                return []
            end = self.capture_elapsed_ms()
            return [CaptureLine(speaker=None, text=text, start_ms=end, end_ms=end)]

        lines: List[CaptureLine] = []
        run: List[dict] = []
        run_speaker: Any = None

        def flush() -> None:
            if not run:
                return
            text = "".join(str(t.get("text", "")) for t in run).strip()
            if text:
                lines.append(
                    CaptureLine(
                        speaker=str(run_speaker) if run_speaker is not None else None,
                        text=text,
                        start_ms=self._offset_ms(run[0].get("start_ms")),
                        end_ms=self._offset_ms(run[-1].get("end_ms")),
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
        return lines

    def _offset_ms(self, token_ms: Any) -> int:
        """Token time → offset from capture start; wall clock when absent."""
        if not isinstance(token_ms, (int, float)):
            return self.capture_elapsed_ms()
        if self._capture_token_origin_ms is None:
            self._capture_token_origin_ms = int(token_ms)
        return max(0, int(token_ms) - self._capture_token_origin_ms)

    async def _capture(self, frame: TranscriptionFrame) -> None:
        """Capture-mode handling of one final frame: stop command or lines."""
        if self._is_stop_command(frame.text):
            logger.info("TranscriptionGate: stop phrase matched, ending capture")
            on_stop = self._on_stop
            if on_stop is not None:
                await on_stop()
            return
        on_line = self._on_line
        if on_line is None:
            return
        for line in self._split_lines(frame):
            await on_line(line)

    # ------------------------------------------------------------------
    # FrameProcessor implementation
    # ------------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # ---- Track bot activity state (for keyword filter) -----------
        if isinstance(frame, LLMFullResponseStartFrame):
            self._llm_active = True

        elif isinstance(frame, LLMFullResponseEndFrame):
            self._llm_active = False

        elif isinstance(frame, BotStartedSpeakingFrame):
            self._tts_active = True

        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._tts_active = False

        # ---- Transcription suppression logic -------------------------
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            # Mode 1: hard mute — drop unconditionally (both final and interim).
            # InterimTranscriptionFrame must also be dropped: TranscriptionUserTurnStartStrategy
            # with use_interim=True fires on interim frames, which triggers an interruption even
            # when the final TranscriptionFrame would have been suppressed.
            if self._muted:
                logger.debug(
                    f"TranscriptionGate: dropping {'interim ' if isinstance(frame, InterimTranscriptionFrame) else ''}"
                    f"transcription (hard mute active)"
                )
                return  # Silently drop

            # Mode 3: capture — finals become transcript lines (or the stop
            # command); interims never reach the turn strategies.
            if self._capturing:
                if isinstance(frame, TranscriptionFrame):
                    await self._capture(frame)
                elif self._on_interim is not None and frame.text.strip():
                    await self._on_interim(frame.text)
                return  # Never downstream while capturing

            # Mode 2: keyword filter — drop matching words while bot is active.
            # Applies to both final TranscriptionFrames and InterimTranscriptionFrames
            if (
                self._keyword_filter_enabled
                and self._is_bot_active()
                and self._keywords
                and self._keyword_matches(frame.text)
            ):
                logger.debug(
                    f"TranscriptionGate: dropping transcription "
                    f"(keyword match, match_type={self._match_type.value}, bot_active=True)"
                )
                return  # Silently drop

        # Pass everything else (and non-suppressed transcriptions) downstream
        await self.push_frame(frame, direction)

import time
from collections import defaultdict
from typing import Any, Dict, Optional

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    MetricsFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import (
    ProcessingMetricsData,
    TextAggregationMetricsData,
    TTFBMetricsData,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.core.logger import logger


class MetricsCollectorProcessor(FrameProcessor):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._turns: list[Dict[str, Any]] = []
        # each run overwrite the one before it.
        self._current_turn_metrics: Dict[str, Dict[str, list[float]]] = defaultdict(
            dict
        )
        self._current_turn_functions: list[Dict[str, Any]] = []
        self._function_starts: Dict[str, float] = {}  # tool_call_id -> start time
        self._frames_seen = set()
        # Turn-level TTFC ("time to first completion"): user turn stop (STT
        # final handed to the LLM) → first aggregated sentence handed to TTS.
        # Measured end to end because the raw LLM ttfb misses everything the
        # caller waits through before the bot can speak: KB retrieval, tool
        # execution, and LLM sentence aggregation. Purely additive — ttft (the
        # raw LLM ttfb) and every other metric keep flowing untouched.
        self._ttfc_start: Optional[float] = None
        self._ttfc_first_sentence_at: Optional[float] = None
        # STT finalization tail: last interim transcript → finalized
        # transcript. With stt_native endpointing this is the observable STT
        # contribution (the speech-end anchor pipecat's own STT ttfb needs
        # never fires with VAD disabled, which is why SonioxSTTService ttfb
        # shows 0.0). Interim/final TranscriptionFrames never REACH this
        # processor in agent mode — the user aggregator swallows them — so an
        # STTTimingTapProcessor placed upstream calls the note_* hooks instead.
        self._stt_last_interim_at: Optional[float] = None
        self._stt_finalize_ms: Optional[float] = None
        # Recognizer tail after the caller stops: user speech stop (VAD stop
        # when BREEZE_BUDDY_ENABLE_VAD, the transport's speech-stop otherwise)
        # → the finalized transcript. This is the endpoint delay + finalize
        # the caller waits through before the turn can even be considered
        # complete in timeout mode. The anchor is UserStoppedSpeakingFrame,
        # which reaches this processor (the raw VAD frames may be consumed
        # upstream); the final lands via the STT timing tap's note_stt_final.
        self._speech_stop_at: Optional[float] = None
        self._stt_vad_final_ms: Optional[float] = None
        # Early-fire moment: user speech stop → the say line queued to TTS at
        # function-NAME decode (ahead of argument decode). The earliest
        # point the reply's speech could start, set by the early-speech
        # router's note_early_say() hook.
        self._early_say_ms: Optional[float] = None
        # Per-generation counter. An agent-to-agent transfer builds a fresh
        # collector, so numbering restarts at 1 and the merged list can repeat a
        # turn number. Left as-is deliberately: the display aligns turns to
        # assistant messages by list order and never reads this field.
        self._turn_count = 1

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # A turn ends when the bot stops speaking, or — for the final turn that
        # ends the call — when the pipeline tears down via EndFrame.
        if isinstance(frame, (BotStoppedSpeakingFrame, EndFrame)):
            self._commit_turn()
        elif isinstance(frame, FunctionCallInProgressFrame):
            self._function_starts[frame.tool_call_id] = time.monotonic()
        elif isinstance(frame, FunctionCallResultFrame):
            started = self._function_starts.pop(frame.tool_call_id, None)
            if started is not None:
                self._current_turn_functions.append(
                    {
                        "name": frame.function_name,
                        "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    }
                )
        elif isinstance(frame, UserStoppedSpeakingFrame):
            # STT final handed to the LLM — the TTFC clock starts here, and
            # the recognizer-tail window opens (speech stop → final).
            self._ttfc_start = time.monotonic()
            self._ttfc_first_sentence_at = None
            self._speech_stop_at = self._ttfc_start
        elif isinstance(frame, MetricsFrame) and frame.id not in self._frames_seen:
            self._frames_seen.add(frame.id)
            for data in frame.data:
                processor = data.processor.split("#")[0]
                if isinstance(data, TTFBMetricsData):
                    self._record(processor, "ttfb_ms", data.value)
                elif isinstance(data, ProcessingMetricsData):
                    self._record(processor, "processing_ms", data.value)
                elif isinstance(data, TextAggregationMetricsData):
                    self._record(processor, "text_aggregation_ms", data.value)

        await self.push_frame(frame, direction)

    def note_tts_request(self) -> None:
        """Record the first aggregated sentence handed to TTS for this turn.

        Wired to the TTS service's ``on_tts_request`` event, which fires after
        sentence aggregation right before synthesis. Turns that start without a
        user utterance (greeting) have no TTFC clock running and are ignored.
        """
        if self._ttfc_start is None:
            return
        if self._ttfc_first_sentence_at is None:
            self._ttfc_first_sentence_at = time.monotonic()

    def note_stt_interim(self) -> None:
        """STT timing tap (upstream of the user aggregator) saw an interim."""
        if self._stt_last_interim_at is None:
            # First interim of a new utterance — fresh measurement window.
            self._stt_finalize_ms = None
            self._stt_vad_final_ms = None
            self._speech_stop_at = None
        self._stt_last_interim_at = time.monotonic()

    def note_stt_final(self) -> None:
        """STT timing tap saw the finalized transcript for the utterance."""
        now = time.monotonic()
        if self._stt_last_interim_at is not None:
            self._stt_finalize_ms = round((now - self._stt_last_interim_at) * 1000, 1)
            self._stt_last_interim_at = None
        if self._speech_stop_at is not None:
            # First final after the speech stop — the recognizer tail. Later
            # finals of the same merged turn (timeout mode) would only
            # restate more speech, not more tail, so keep the first.
            self._stt_vad_final_ms = round((now - self._speech_stop_at) * 1000, 1)
            self._speech_stop_at = None

    def note_early_say(self) -> None:
        """Early-speech router queued a say line at function-name decode.

        Measures user speech stop → earliest speakable moment, the head start
        the early-fire buys over waiting for the full argument decode.
        """
        if self._ttfc_start is not None and self._early_say_ms is None:
            now = time.monotonic()
            if now >= self._ttfc_start:
                self._early_say_ms = round((now - self._ttfc_start) * 1000, 1)

    def _record(self, processor: str, metric: str, seconds: float) -> None:
        """Append a measurement, preserving every run within the turn."""
        self._current_turn_metrics[processor].setdefault(metric, []).append(
            round(seconds * 1000, 1)
        )

    def _commit_turn(self) -> None:
        self._frames_seen.clear()

        ttfc_ms: Optional[float] = None
        if self._ttfc_start is not None and self._ttfc_first_sentence_at is not None:
            ttfc_ms = round((self._ttfc_first_sentence_at - self._ttfc_start) * 1000, 1)
        # Reset unconditionally: a turn interrupted before the bot could speak
        # reports no ttfc rather than a stale measurement carried forward.
        self._ttfc_start = None
        self._ttfc_first_sentence_at = None

        if (
            not self._current_turn_metrics
            and not self._current_turn_functions
            and ttfc_ms is None
            and self._stt_finalize_ms is None
            and self._stt_vad_final_ms is None
            and self._early_say_ms is None
        ):
            return

        turn: Dict[str, Any] = {
            "turn": self._turn_count,
            "processors": {
                name: dict(metrics)
                for name, metrics in self._current_turn_metrics.items()
            },
        }
        if ttfc_ms is not None:
            turn["ttfc_ms"] = ttfc_ms
        if self._stt_finalize_ms is not None:
            turn["stt_finalize_ms"] = self._stt_finalize_ms
        if self._stt_vad_final_ms is not None:
            turn["stt_vad_final_ms"] = self._stt_vad_final_ms
        if self._early_say_ms is not None:
            turn["early_say_ms"] = self._early_say_ms
        if self._current_turn_functions:
            turn["functions"] = self._current_turn_functions

        self._log_turn(turn)

        self._turns.append(turn)
        self._turn_count += 1
        self._current_turn_metrics = defaultdict(dict)
        self._current_turn_functions = []
        self._stt_finalize_ms = None
        self._stt_last_interim_at = None
        self._speech_stop_at = None
        self._stt_vad_final_ms = None
        self._early_say_ms = None

    def _log_turn(self, turn: Dict[str, Any]) -> None:
        """One INFO line per turn with the latency breakdown, so bottlenecks
        are visible in call logs without opening the stored metadata. Purely
        synchronous (logger only) — never blocks the pipeline."""

        def last(processor: str, metric: str) -> Optional[float]:
            values = turn["processors"].get(processor, {}).get(metric)
            # The prefill request's metrics land in turn 1 as a leading 0.0 —
            # the real measurement is the last value.
            return values[-1] if values else None

        def first(processor: str, metric: str) -> Optional[float]:
            values = turn["processors"].get(processor, {}).get(metric)
            return values[0] if values else None

        functions = turn.get("functions") or []
        fn = (
            f"{functions[-1]['name']}({functions[-1]['latency_ms']}ms)"
            if functions
            else None
        )
        parts = [
            f"turn={turn['turn']}",
            f"stt_finalize={turn.get('stt_finalize_ms', '?')}ms",
            f"vad_to_final={turn.get('stt_vad_final_ms', '?')}ms",
            f"llm_ttfb={last('OpenAILLMService', 'ttfb_ms') or '?'}ms",
            f"early_say={turn.get('early_say_ms', '?')}ms",
            f"llm_complete={last('OpenAILLMService', 'processing_ms') or '?'}ms",
            f"tool={fn or '?'}",
            f"tts_ttfb={first('DragonTTSService', 'ttfb_ms') or '?'}ms",
            f"ttfc={turn.get('ttfc_ms', '?')}ms",
        ]
        logger.info(f"[TURN METRICS] " + " ".join(parts))

    def get_metrics(self) -> list[Dict[str, Any]]:
        """Return the aggregated metrics grouped by conversational turn."""
        self._commit_turn()
        return self._turns

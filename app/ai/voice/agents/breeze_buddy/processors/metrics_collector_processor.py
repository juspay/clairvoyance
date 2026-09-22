import time
from collections import defaultdict
from typing import Any, Dict, Optional

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import (
    ProcessingMetricsData,
    TextAggregationMetricsData,
    TTFBMetricsData,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Frames that mark a point on the turn's timeline, and the name each takes.
_FRAME_EVENT_NAMES: list[tuple[type, str]] = [
    (UserStartedSpeakingFrame, "user_started_speaking"),
    (UserStoppedSpeakingFrame, "user_stopped_speaking"),
    (LLMFullResponseStartFrame, "llm_response_start"),
    (LLMFullResponseEndFrame, "llm_response_end"),
    (TTSStartedFrame, "tts_request"),
    (TTSStoppedFrame, "tts_done"),
    (BotStartedSpeakingFrame, "bot_started_speaking"),
    (BotStoppedSpeakingFrame, "bot_stopped_speaking"),
]


class MetricsCollectorProcessor(FrameProcessor):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._turns: list[Dict[str, Any]] = []
        # each run overwrite the one before it.
        self._current_turn_metrics: Dict[str, Dict[str, list[float]]] = defaultdict(
            dict
        )
        self._current_turn_functions: list[Dict[str, Any]] = []
        self._function_starts: Dict[str, int] = {}  # tool_call_id -> start ns
        self._metrics_frames_seen: set[int] = set()
        # The turn's timeline, and the clock it is measured against. The clock
        # starts on the turn's FIRST event whoever it belongs to: an outbound
        # greeting opens the call with no user in sight.
        self._timeline: list[Dict[str, Any]] = []
        self._turn_started_at: Optional[int] = None
        self._previous_event_at_ms: int = 0
        self._was_interrupted = False
        self._first_audio_marked = False
        self._first_token_marked = False
        self._stt_final_marked = False
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
            # Mark before committing, or the event lands on the next turn.
            self._mark_frame(frame)
            self._commit_turn()
        elif isinstance(frame, InterruptionFrame):
            # NOT a barge-in signal on its own. The user aggregator calls
            # broadcast_interruption() on EVERY user turn start when
            # enable_interruptions is set, which is its default and what this
            # repo builds its turn strategies with — so this frame arrives on
            # every ordinary turn. It only means the caller cut the bot off
            # when the bot was mid-sentence: already started, not yet stopped.
            if self._is_bot_speaking():
                self._was_interrupted = True
                self._add_event("user_interrupted")
        elif isinstance(frame, TTSAudioRawFrame):
            if not self._first_audio_marked:
                self._first_audio_marked = True
                self._add_event("tts_first_audio")
        elif isinstance(frame, FunctionCallInProgressFrame):
            self._function_starts[frame.tool_call_id] = self._now_ns()
            self._add_event("function_call_started", name=frame.function_name)
        elif isinstance(frame, FunctionCallResultFrame):
            self._add_event("function_call_result", name=frame.function_name)
            started = self._function_starts.pop(frame.tool_call_id, None)
            if started is not None:
                self._current_turn_functions.append(
                    {
                        "name": frame.function_name,
                        "latency_ms": round((self._now_ns() - started) / 1_000_000, 1),
                    }
                )
        elif (
            isinstance(frame, MetricsFrame)
            and frame.id not in self._metrics_frames_seen
        ):
            self._metrics_frames_seen.add(frame.id)
            for data in frame.data:
                processor = data.processor.split("#")[0]
                if isinstance(data, TTFBMetricsData):
                    self._record_processor_metric(processor, "ttfb_ms", data.value)
                elif isinstance(data, ProcessingMetricsData):
                    self._record_processor_metric(
                        processor, "processing_ms", data.value
                    )
                elif isinstance(data, TextAggregationMetricsData):
                    self._record_processor_metric(
                        processor, "text_aggregation_ms", data.value
                    )
        else:
            self._mark_frame(frame)

        await self.push_frame(frame, direction)

    def _is_bot_speaking(self) -> bool:
        """True when the bot has started this turn's reply and not finished.

        The second half never fires from the one call site today —
        BotStoppedSpeakingFrame commits and resets the turn in the same
        breath, so it is never still on the timeline when this is asked. Kept
        because the method answers a question, not a call site.
        """
        started = self._time_of_first("bot_started_speaking")
        return (
            started is not None and self._time_of_first("bot_stopped_speaking") is None
        )

    def _note(self, event: str, marked_attr: str, at_ns: Optional[int]) -> None:
        """Record an observer-reported moment, once per turn.

        Two guards, both because an observer is not the pipeline:

        * ONE-SHOT. An observer is called for every source -> destination HOP,
          not once per frame, and a TranscriptionFrame crosses several hops
          before the user aggregator swallows it. Without this the timeline
          would carry a row per hop.
        * NEVER OPENS A TURN. The observer drains its own queue, so a report
          can arrive after the turn it belongs to has already been committed.
          Letting a late report start the next turn would make a moment from
          the PREVIOUS turn that turn's zero, shifting every at_ms, gap_ms and
          response_latency_ms after it. A report with no open turn is dropped.
        """
        if self._turn_started_at is None or getattr(self, marked_attr):
            return
        setattr(self, marked_attr, True)
        self._add_event(event, at_ns)

    def note_stt_final(self, at_ns: Optional[int] = None) -> None:
        """Called by the observer: the transcript for this turn is final."""
        self._note("stt_final", "_stt_final_marked", at_ns)

    def note_llm_first_token(self, at_ns: Optional[int] = None) -> None:
        """Called by the observer on the turn's first LLM text token."""
        self._note("llm_first_token", "_first_token_marked", at_ns)

    def _mark_frame(self, frame: Frame) -> None:
        """Mark `frame` on the timeline if it is one of the known events."""
        for frame_type, event in _FRAME_EVENT_NAMES:
            if isinstance(frame, frame_type):
                self._add_event(event)
                return

    def _now_ns(self) -> int:
        """The pipeline clock, or the raw monotonic clock before setup runs.

        Both are time.monotonic_ns() based — SystemClock.get_time() returns
        monotonic_ns() minus the pipeline's start — so a reading taken here
        and a FramePushed.timestamp handed to the observer sit on the SAME
        timeline. Mixing time.monotonic() seconds with the clock's
        nanoseconds would not.
        """
        clock = getattr(self, "_clock", None)
        return clock.get_time() if clock else time.monotonic_ns()

    def _add_event(self, event: str, at_ns: Optional[int] = None, **extra: Any) -> None:
        """Append `event` with its offset from turn start and the gap before it.

        `at_ns` is when the event actually happened, for a caller that knows
        better than "now" — an observer runs on its own drained queue and can
        be several hundred milliseconds behind the push it is reporting.

        The turn's first event opens the clock and so carries no gap. Offsets
        are whole milliseconds: sub-millisecond precision is noise on a voice
        call, and a ".0" on every value is dead weight in a column read whole.
        """
        now = at_ns if at_ns is not None else self._now_ns()
        if self._turn_started_at is None:
            self._turn_started_at = now
            self._previous_event_at_ms = 0
            self._timeline.append({"event": event, "at_ms": 0, **extra})
            return

        # An observer stamps an event with its PUSH time on an upstream link,
        # while the origin was taken when the collector — the last processor —
        # handled the turn's first frame. A push can therefore predate the
        # origin by a few ms. Clamped so at_ms never goes negative, and the
        # gap is taken against the clamped value so gaps keep summing to
        # offsets for the rest of the turn.
        at_ms = max(0, round((now - self._turn_started_at) / 1_000_000))
        self._timeline.append(
            {
                "event": event,
                "at_ms": at_ms,
                "gap_ms": max(0, at_ms - self._previous_event_at_ms),
                **extra,
            }
        )
        self._previous_event_at_ms = max(self._previous_event_at_ms, at_ms)

    def _record_processor_metric(
        self, processor: str, metric: str, seconds: float
    ) -> None:
        """Append a measurement, preserving every run within the turn."""
        self._current_turn_metrics[processor].setdefault(metric, []).append(
            round(seconds * 1000, 1)
        )

    def _time_of_first(self, event: str) -> Optional[int]:
        """Offset of the first `event` on the timeline, if it happened at all."""
        return next(
            (entry["at_ms"] for entry in self._timeline if entry["event"] == event),
            None,
        )

    def _durations_between(self, start_event: str, end_event: str) -> list[int]:
        """Every start-to-end duration on the timeline, in order.

        Speech events repeat within a turn — a caller talking over the bot
        stops speaking AFTER the bot started. Reading them off a flat
        {event: at_ms} map keeps only the last of each and silently subtracts
        them out of order, which is how a duration goes negative.
        """
        durations: list[int] = []
        started_at_ms: Optional[int] = None
        for entry in self._timeline:
            if entry["event"] == start_event and started_at_ms is None:
                started_at_ms = entry["at_ms"]
            elif entry["event"] == end_event and started_at_ms is not None:
                durations.append(entry["at_ms"] - started_at_ms)
                started_at_ms = None
        return durations

    def _stage_ttfb(self, stage: str) -> Optional[int]:
        """First TTFB reading for the processor serving `stage`.

        Processor keys are Pipecat CLASS names — AzureLLMService,
        ElevenLabsTTSService, SonioxSTTServiceWithEndpointDelay — never the
        bare stage. They change with the configured provider, so the stage is
        matched as a substring rather than looked up by a fixed key.
        """
        for name, metrics in self._current_turn_metrics.items():
            if stage.lower() in name.lower():
                readings = metrics.get("ttfb_ms")
                if readings:
                    return round(readings[0])
        return None

    def _summarize(self) -> Dict[str, Any]:
        """Derive the headline numbers a reader wants without doing subtraction."""
        summary: Dict[str, Any] = {}

        # Measured from the caller's last word BEFORE the bot answered, not
        # their last word of the turn. Omitted on a turn the bot opened — a
        # greeting answers nobody, and a zero would drag every average down.
        bot_started_at_ms = self._time_of_first("bot_started_speaking")
        if bot_started_at_ms is not None:
            user_stopped_at_ms = max(
                (
                    entry["at_ms"]
                    for entry in self._timeline
                    if entry["event"] == "user_stopped_speaking"
                    and entry["at_ms"] <= bot_started_at_ms
                ),
                default=None,
            )
            if user_stopped_at_ms is not None:
                summary["response_latency_ms"] = bot_started_at_ms - user_stopped_at_ms

        # Taken from the provider's own TTFB metric, not derived from the
        # timeline: LLMFullResponseStartFrame is pushed BEFORE the request on
        # Anthropic and AFTER the first token on OpenAI, so no arithmetic on it
        # means the same thing across providers. The service measures TTFB
        # inside itself, which is correct by construction for all of them.
        llm_ttfb_ms = self._stage_ttfb("LLM")
        if llm_ttfb_ms is not None:
            summary["llm_ttft_ms"] = llm_ttfb_ms

        for field, start_event, end_event in (
            ("user_speech_ms", "user_started_speaking", "user_stopped_speaking"),
            ("bot_speech_ms", "bot_started_speaking", "bot_stopped_speaking"),
            ("llm_total_ms", "llm_response_start", "llm_response_end"),
            ("tts_total_ms", "tts_request", "tts_done"),
        ):
            durations = self._durations_between(start_event, end_event)
            if durations:
                summary[field] = sum(durations)

        if self._was_interrupted:
            summary["interrupted"] = True
            # How long the bot got before being cut off. This is the number that
            # says WHY: a caller cutting in after 300ms is impatient or the bot
            # misheard; one cutting in after 8s means the bot is rambling and
            # the prompt needs shortening. `bot_speech_ms` alone cannot tell
            # those apart, since a cut-off turn and a complete one look alike.
            bot_started_at_ms = self._time_of_first("bot_started_speaking")
            interrupted_at_ms = self._time_of_first("user_interrupted")
            if (
                bot_started_at_ms is not None
                and interrupted_at_ms is not None
                and interrupted_at_ms >= bot_started_at_ms
            ):
                # Omitted rather than clamped: a max(0, ...) here would report
                # "cut off after 0ms", which reads as a real measurement.
                summary["time_to_interruption_ms"] = (
                    interrupted_at_ms - bot_started_at_ms
                )
        return summary

    def _commit_turn(self) -> None:
        self._metrics_frames_seen.clear()

        # The emission rule is UNCHANGED from before the timeline existed: a
        # turn is emitted only when it carries processor metrics or a function
        # call. The console aligns turns to assistant messages BY LIST ORDER,
        # so emitting a turn that previously produced nothing — an outbound
        # greeting plays pre-generated audio and raises no MetricsFrame —
        # would shift every message against the wrong turn. The timeline rides
        # on the turns that already existed; it never adds one.
        if not self._current_turn_metrics and not self._current_turn_functions:
            self._reset_turn()
            return

        turn: Dict[str, Any] = {
            "turn": self._turn_count,
            "processors": {
                name: dict(metrics)
                for name, metrics in self._current_turn_metrics.items()
            },
        }
        if self._current_turn_functions:
            turn["functions"] = self._current_turn_functions
        if self._timeline:
            turn["timeline"] = self._timeline
            summary = self._summarize()
            if summary:
                turn["summary"] = summary

        self._turns.append(turn)
        self._turn_count += 1
        self._reset_turn()

    def _reset_turn(self) -> None:
        """Clear the scratchpad so the next turn starts on its own clock."""
        self._current_turn_metrics = defaultdict(dict)
        self._current_turn_functions = []
        self._timeline = []
        self._turn_started_at = None
        self._previous_event_at_ms = 0
        self._was_interrupted = False
        self._first_audio_marked = False
        self._first_token_marked = False
        self._stt_final_marked = False

    def get_metrics(self) -> list[Dict[str, Any]]:
        """Return the aggregated metrics grouped by conversational turn."""
        self._commit_turn()
        return self._turns


class TimelineObserver(BaseObserver):
    """Reports the two moments the collector cannot see from where it sits.

    The collector is the last processor before the transport output, and by
    then both moments are gone: the user aggregator folds TranscriptionFrame
    into conversation history, and the TTS service buffers LLMTextFrame into
    whole sentences before forwarding anything — the content survives, the
    arrival time does not. Neither stage can simply forward the original, as a
    frame is payload and not an announcement: anything downstream would act on
    it and record the reply twice.

    An observer sees every frame at every link WITHOUT being wired into the
    pipeline, which is exactly the side channel this needs. It only ever reads.
    """

    def __init__(self, collector: MetricsCollectorProcessor, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._collector = collector

    async def on_push_frame(self, data: FramePushed) -> None:
        """Called for every frame handed from one processor to the next.

        `data.timestamp` is the pipeline clock at the moment of the PUSH.
        This runs on a queue drained by its own task, so "now" here can be
        well after the fact; reporting the push time keeps the number honest.

        Nothing may escape. Pipecat's proxy task handler wraps this in no
        try/except: an exception kills the draining task while the queue it
        fed keeps filling with every frame at every hop — audio payloads
        included — for the rest of the call.
        """
        try:
            frame = data.frame
            if isinstance(frame, LLMTextFrame):
                self._collector.note_llm_first_token(data.timestamp)
            elif isinstance(frame, TranscriptionFrame):
                # Not gated on `finalized`. That field defaults to False and
                # only Soniox sets it; Deepgram leaves it False on the normal
                # endpointing path, so gating here meant no stt_final at all
                # on Deepgram calls. A TranscriptionFrame is already the final
                # text — an in-progress one is an InterimTranscriptionFrame,
                # which is a separate class and does not match here.
                self._collector.note_stt_final(data.timestamp)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"TimelineObserver dropped a frame report: {e}")

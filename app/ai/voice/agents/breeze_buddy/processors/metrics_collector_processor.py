import time
from collections import defaultdict
from typing import Any, Dict

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    MetricsFrame,
)
from pipecat.metrics.metrics import (
    ProcessingMetricsData,
    TextAggregationMetricsData,
    TTFBMetricsData,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


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

    def _record(self, processor: str, metric: str, seconds: float) -> None:
        """Append a measurement, preserving every run within the turn."""
        self._current_turn_metrics[processor].setdefault(metric, []).append(
            round(seconds * 1000, 1)
        )

    def _commit_turn(self) -> None:
        self._frames_seen.clear()

        if not self._current_turn_metrics and not self._current_turn_functions:
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

        self._turns.append(turn)
        self._turn_count += 1
        self._current_turn_metrics = defaultdict(dict)
        self._current_turn_functions = []

    def get_metrics(self) -> list[Dict[str, Any]]:
        """Return the aggregated metrics grouped by conversational turn."""
        self._commit_turn()
        return self._turns

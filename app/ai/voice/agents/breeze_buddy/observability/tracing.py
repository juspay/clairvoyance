"""
Custom tracing implementation for Breeze Buddy.

This module provides manual tracing using custom observers instead of Pipecat's
built-in tracing. This workaround is needed because Pipecat's TurnContextProvider
is a singleton that causes trace mixing when multiple concurrent calls run
in the same process.

Each pipeline manages a separate parent span, and spans for TTS/LLM/STT are
explicitly created under their appropriate conversation span.
"""

import json
from collections import deque
from typing import Optional

from loguru import logger
from opentelemetry import trace
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.metrics.metrics import (
    LLMUsageMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.pipeline import PipelineSink
from pipecat.processors.aggregators.openai_llm_context import (
    OpenAILLMContext,
    OpenAILLMContextFrame,
)
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.tracing.service_attributes import (
    _get_gen_ai_system_from_service_name,
)

# NOTE: Do NOT create tracer at module level!
# The tracer must be obtained AFTER setup_tracing() configures the TracerProvider.
# Each class gets the tracer at runtime to ensure it uses the configured exporter.


def _get_tracer():
    """Get the tracer at runtime, after TracerProvider is configured."""
    return trace.get_tracer("breeze-buddy")


class LLMTracingObserver(BaseObserver):
    """
    Observer that creates spans for LLM operations.
    Each span is created as a child of the provided parent_span.
    """

    def __init__(
        self,
        parent_span: trace.Span,
        max_frames: int = 100,
        **kwargs,
    ):
        self._parent_span = parent_span
        self._llm_span: Optional[trace.Span] = None
        self._llm_output = ""

        # Track processed frames to avoid duplicates
        self._processed_frames = set()
        self._frame_history = deque(maxlen=max_frames)

        super().__init__(**kwargs)

    def _start_llm(self, service: LLMService, context: OpenAILLMContext):
        self._llm_output = ""
        with trace.use_span(self._parent_span):
            self._llm_span = _get_tracer().start_span("llm")

            tools = context.tools
            serialized_tools = None

            if tools:
                try:
                    serialized_tools = json.dumps(tools)
                except Exception:
                    serialized_tools = "Error serializing tools"

            serialized_messages = context.get_messages_json()

            self._llm_span.set_attributes(
                {
                    "gen_ai.system": _get_gen_ai_system_from_service_name(
                        service.__class__.__name__
                    ),
                    "gen_ai.request.model": service.model_name,
                    "gen_ai.operation.name": "chat",
                    "gen_ai.output.type": "text",
                    "llm.messages": serialized_messages,
                    "llm.tools": serialized_tools,
                }
            )

    def _end_llm(self):
        if not self._llm_span:
            logger.debug("Attempted to end LLM span without an active LLM span")
            return
        self._llm_span.set_attribute("llm.output", self._llm_output)
        self._llm_span.end()
        self._llm_span = None

    def _add_llm_text(self, text: str):
        if not self._llm_span:
            logger.debug("Attempted to add LLM text without an active LLM span")
            return
        self._llm_span.add_event("llm.text", attributes={"text": text})
        self._llm_output += text

    def _add_ttfb_metric(self, metric: TTFBMetricsData):
        if not self._llm_span:
            logger.debug("Attempted to add TTFB metric without an active LLM span")
            return
        self._llm_span.set_attribute("metrics.ttfb", metric.value)

    def _add_llm_usage_metric(self, metric: LLMUsageMetricsData):
        if not self._llm_span:
            logger.debug("Attempted to add LLM usage metric without an active LLM span")
            return
        self._llm_span.set_attribute(
            "gen_ai.usage.input_tokens", metric.value.prompt_tokens
        )
        self._llm_span.set_attribute(
            "gen_ai.usage.output_tokens", metric.value.completion_tokens
        )

    def _cleanup(self):
        self._end_llm()

    async def on_push_frame(self, data: FramePushed):
        src = data.source
        dst = data.destination
        frame = data.frame

        if not isinstance(src, LLMService) and not isinstance(dst, LLMService):
            return

        # Skip already processed frames
        if data.frame.id in self._processed_frames:
            return

        self._processed_frames.add(data.frame.id)
        self._frame_history.append(data.frame.id)

        # Rebuild the set if we've exceeded our history size
        if len(self._processed_frames) > len(self._frame_history):
            self._processed_frames = set(self._frame_history)

        if isinstance(frame, (EndFrame, CancelFrame)):
            self._cleanup()

        if isinstance(frame, LLMFullResponseStartFrame):
            pass

        if isinstance(frame, LLMFullResponseEndFrame):
            self._end_llm()

        if isinstance(frame, OpenAILLMContextFrame):
            if not isinstance(dst, LLMService):
                logger.warning("Destination is not a LLM service")
                return
            self._start_llm(service=dst, context=frame.context)

        if isinstance(frame, LLMTextFrame):
            self._add_llm_text(frame.text)

        if isinstance(frame, MetricsFrame):
            for metric in frame.data:
                if isinstance(metric, TTFBMetricsData):
                    self._add_ttfb_metric(metric)
                if isinstance(metric, LLMUsageMetricsData):
                    self._add_llm_usage_metric(metric)


class TTSTracingObserver(BaseObserver):
    """
    Observer that creates spans for TTS operations.
    Each span is created as a child of the provided parent_span.
    """

    def __init__(
        self,
        parent_span: trace.Span,
        max_frames: int = 100,
        **kwargs,
    ):
        self._parent_span = parent_span
        self._tts_span: Optional[trace.Span] = None

        # Track processed frames to avoid duplicates
        self._processed_frames = set()
        self._frame_history = deque(maxlen=max_frames)

        super().__init__(**kwargs)

    def _start_tts(self, service: TTSService):
        with trace.use_span(self._parent_span):
            self._tts_span = _get_tracer().start_span("tts")
            self._tts_span.set_attributes(
                {
                    "tts.model": service.model_name,
                }
            )

    def _end_tts(self):
        if not self._tts_span:
            logger.debug("Attempted to end TTS span without an active TTS span")
            return
        self._tts_span.end()
        self._tts_span = None

    def _add_tts_text(self, text: str):
        if not self._tts_span:
            logger.debug("Attempted to add TTS text without an active TTS span")
            return
        self._tts_span.add_event("tts.text", attributes={"text": text})

    def _add_ttfb_metric(self, metric: TTFBMetricsData):
        if not self._tts_span:
            logger.debug("Attempted to add TTFB metric without an active TTS span")
            return
        self._tts_span.set_attribute("metrics.ttfb", metric.value)

    def _add_tts_usage_metric(self, metric: TTSUsageMetricsData):
        if not self._tts_span:
            logger.debug("Attempted to add TTS usage metric without an active TTS span")
            return
        self._tts_span.set_attribute("metrics.tts_usage", metric.value)

    def _cleanup(self):
        self._end_tts()

    async def on_push_frame(self, data: FramePushed):
        src = data.source
        dst = data.destination
        frame = data.frame

        if not isinstance(src, TTSService) and not isinstance(dst, TTSService):
            return

        # Skip already processed frames
        if data.frame.id in self._processed_frames:
            return

        self._processed_frames.add(data.frame.id)
        self._frame_history.append(data.frame.id)

        # Rebuild the set if we've exceeded our history size
        if len(self._processed_frames) > len(self._frame_history):
            self._processed_frames = set(self._frame_history)

        if isinstance(frame, (EndFrame, CancelFrame)):
            self._cleanup()

        if isinstance(frame, TTSStartedFrame):
            if not isinstance(src, TTSService):
                logger.warning("Source is not a TTS service")
                return
            self._start_tts(service=src)

        if isinstance(frame, TTSStoppedFrame):
            self._end_tts()

        if isinstance(frame, TTSTextFrame):
            self._add_tts_text(frame.text)

        if isinstance(frame, MetricsFrame):
            for metric in frame.data:
                if isinstance(metric, TTFBMetricsData):
                    self._add_ttfb_metric(metric)
                if isinstance(metric, TTSUsageMetricsData):
                    self._add_tts_usage_metric(metric)


class STTTracingObserver(BaseObserver):
    """
    Observer that creates spans for STT (speech-to-text) operations.
    Each span is created as a child of the provided parent_span.
    """

    def __init__(
        self,
        parent_span: trace.Span,
        max_frames: int = 100,
        **kwargs,
    ):
        self._parent_span = parent_span
        self._stt_span: Optional[trace.Span] = None

        # Track processed frames to avoid duplicates
        self._processed_frames = set()
        self._frame_history = deque(maxlen=max_frames)

        super().__init__(**kwargs)

    def _handle_transcription(self, service: STTService, text: str):
        with trace.use_span(self._parent_span):
            with _get_tracer().start_as_current_span("stt") as stt_span:
                stt_span.set_attributes(
                    {
                        "stt.text": text,
                        "stt.service": service.__class__.__name__,
                        "stt.model": service.model_name,
                    }
                )

    def _cleanup(self):
        pass

    async def on_push_frame(self, data: FramePushed):
        src = data.source
        dst = data.destination
        frame = data.frame

        if not isinstance(src, STTService) and not isinstance(dst, STTService):
            return

        # Skip already processed frames
        if data.frame.id in self._processed_frames:
            return

        self._processed_frames.add(data.frame.id)
        self._frame_history.append(data.frame.id)

        # Rebuild the set if we've exceeded our history size
        if len(self._processed_frames) > len(self._frame_history):
            self._processed_frames = set(self._frame_history)

        if isinstance(frame, (EndFrame, CancelFrame)):
            self._cleanup()

        if isinstance(frame, TranscriptionFrame):
            if not isinstance(src, STTService):
                logger.warning("Source is not a STT service")
                return
            self._handle_transcription(service=src, text=frame.text)


class PipelineObserver(BaseObserver):
    """
    Observer that handles pipeline-level events (EndFrame, CancelFrame).
    Uses event handlers to notify when pipeline ends/cancels.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._register_event_handler("on_pipeline_ended")
        self._register_event_handler("on_pipeline_cancelled")

    async def _pipeline_ended(self, *args, **kwargs):
        await self._call_event_handler("on_pipeline_ended", *args, **kwargs)

    async def _pipeline_cancelled(self, *args, **kwargs):
        await self._call_event_handler("on_pipeline_cancelled", *args, **kwargs)

    async def on_push_frame(self, data: FramePushed):
        if not isinstance(data.destination, PipelineSink):
            return

        if isinstance(data.frame, EndFrame):
            await self._pipeline_ended()

        if isinstance(data.frame, CancelFrame):
            await self._pipeline_cancelled()


class PipelineTracing:
    """
    Manages tracing for a single pipeline instance.

    This creates:
    - An overall parent span for the entire pipeline
    - Observers that manage child spans for individual components (LLM, TTS, STT)

    Usage:
        pipeline_tracing = PipelineTracing(
            trace_llm=True,
            trace_tts=True,
            trace_stt=True
        )

        task = PipelineTask(
            pipeline,
            observers=[*pipeline_tracing.observers],
        )
    """

    span: trace.Span
    observers: list[BaseObserver]

    def __init__(
        self,
        trace_llm: bool = True,
        trace_tts: bool = True,
        trace_stt: bool = True,
        conversation_id: Optional[str] = None,
    ):
        """
        Initialize tracing for a pipeline.

        Args:
            trace_llm: Whether to create spans for LLM operations
            trace_tts: Whether to create spans for TTS operations
            trace_stt: Whether to create spans for STT operations
            conversation_id: Optional conversation ID for the span name
        """
        span_name = conversation_id or "pipeline"
        tracer = _get_tracer()  # Get tracer at runtime, after TracerProvider is configured
        span = tracer.start_span(span_name)

        observers: list[BaseObserver] = []

        # Create pipeline observer for lifecycle events
        pipeline_observer = PipelineObserver()

        # Register event handlers using decorator pattern
        # These closures capture the local 'span' variable
        @pipeline_observer.event_handler("on_pipeline_ended")
        async def on_pipeline_ended(observer):
            logger.debug("Pipeline ended, closing parent span")
            span.end()

        @pipeline_observer.event_handler("on_pipeline_cancelled")
        async def on_pipeline_cancelled(observer):
            logger.debug("Pipeline cancelled, closing parent span")
            span.end()

        observers.append(pipeline_observer)

        # Add component-specific observers
        if trace_llm:
            observers.append(LLMTracingObserver(parent_span=span))
        if trace_tts:
            observers.append(TTSTracingObserver(parent_span=span))
        if trace_stt:
            observers.append(STTTracingObserver(parent_span=span))

        self.span = span
        self.observers = observers

    def set_span_attributes(self, attributes: dict):
        """Set attributes on the parent span."""
        self.span.set_attributes(attributes)

    def add_event(self, name: str, attributes: Optional[dict] = None):
        """Add an event to the parent span."""
        self.span.add_event(name, attributes=attributes or {})

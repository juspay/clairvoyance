import time
import json
from typing import Dict, Any

from opentelemetry import trace
from app.core import config
from app.core.logger import logger
from pipecat.frames.frames import Frame, FunctionCallInProgressFrame, FunctionCallResultFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIServerMessageFrame


# Custom LLMSpyProcessor for streaming function call events
class LLMSpyProcessor(FrameProcessor):
    """Intercepts function call frames to emit RTVI server messages for start and result."""

    def __init__(self, rtvi: RTVIProcessor, name: str = "LLMSpyProcessor"):
        super().__init__(name=name)
        self._rtvi = rtvi

        # Tracing setup
        self._tracer = trace.get_tracer("pipecat.tools") if config.ENABLE_TRACING else None
        self._active_spans: Dict[str, Any] = {}  # tool_call_id -> span

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Emit RTVI server messages for function call frames."""
        await super().process_frame(frame, direction)

        if isinstance(frame, FunctionCallInProgressFrame):
            # Start tracing span
            if self._tracer:
                span = self._tracer.start_span(f"Tool: {frame.function_name}", kind=trace.SpanKind.CLIENT)
                span.set_attributes({
                    "tool.name": frame.function_name,
                    "tool.args_json": json.dumps(frame.arguments, default=str)[:4096]
                })
                self._active_spans[frame.tool_call_id] = span

            logger.info(f"Function call started: {frame.function_name} with args: {frame.arguments}")
            await self._rtvi.push_frame(
                RTVIServerMessageFrame(
                    data={
                        "type": "tool-call-start",
                        "payload": {
                            "toolCallId": frame.tool_call_id,
                            "functionName": frame.function_name,
                            "arguments": frame.arguments,
                            "timestamp": int(time.time() * 1000)
                        }
                    }
                )
            )
        elif isinstance(frame, FunctionCallResultFrame):
            # End tracing span
            if self._tracer and frame.tool_call_id in self._active_spans:
                span = self._active_spans.pop(frame.tool_call_id)
                span.set_attribute("tool.result", json.dumps(frame.result, default=str)[:4096])
                span.end()
            
            logger.info(f"Function call result: {frame.function_name} with result: {frame.result}")
            await self._rtvi.push_frame(
                RTVIServerMessageFrame(
                    data={
                        "type": "tool-call-result",
                        "payload": {
                            "toolCallId": frame.tool_call_id,
                            "functionName": frame.function_name,
                            "arguments": frame.arguments,
                            "result": frame.result,
                            "timestamp": int(time.time() * 1000)
                        }
                    }
                )
            )

        await self.push_frame(frame, direction)
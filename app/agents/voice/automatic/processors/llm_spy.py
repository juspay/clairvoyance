"""
LLM Spy Processor for intercepting function calls and conversation events.
Lightweight frame processor that handles RTVI communication and function confirmations.
"""

import time
import asyncio
from typing import Dict, Optional, Any
import json
from opentelemetry import trace
from app.core import config
from app.core.logger import logger
from pipecat.frames.frames import (
    Frame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    TextFrame
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIServerMessageFrame
from ..services.markdown import sanitize_markdown


# Global RTVI processor reference for function confirmations
_rtvi_processor = None

# Global storage for pending confirmations with thread safety
import threading
_pending_confirmations: Dict[str, asyncio.Future] = {}
_confirmations_lock = threading.Lock()

def get_rtvi_processor():
    """Get the global RTVI processor instance for function confirmations"""
    return _rtvi_processor

def set_rtvi_processor(rtvi):
    """Set the global RTVI processor instance"""
    global _rtvi_processor
    _rtvi_processor = rtvi

def register_pending_confirmation(confirmation_id: str) -> None:
    """Register a pending confirmation that awaits user response via RTVI"""
    with _confirmations_lock:
        _pending_confirmations[confirmation_id] = asyncio.Future()
        logger.debug(f"Registered pending confirmation: {confirmation_id}")

async def wait_for_confirmation_response(confirmation_id: str, timeout_seconds: int = 30) -> Optional[Dict]:
    """Wait for confirmation response via RTVI events"""
    with _confirmations_lock:
        if confirmation_id not in _pending_confirmations:
            logger.error(f"No pending confirmation found for ID: {confirmation_id}")
            return None
        future = _pending_confirmations[confirmation_id]

    try:
        logger.debug(f"Waiting for confirmation response {confirmation_id} with timeout {timeout_seconds}s")
        # Wait for the response with timeout
        response = await asyncio.wait_for(future, timeout=timeout_seconds)
        logger.debug(f"Received confirmation response for {confirmation_id}: {response}")
        return response
    except asyncio.TimeoutError:
        logger.warning(f"Confirmation timeout for {confirmation_id} after {timeout_seconds}s")
        return {"approved": False, "reason": "timeout"}
    except Exception as e:
        logger.error(f"Error waiting for confirmation {confirmation_id}: {e}")
        return {"approved": False, "reason": "error"}
    finally:
        # Clean up the pending confirmation
        with _confirmations_lock:
            removed = _pending_confirmations.pop(confirmation_id, None)
            if removed:
                logger.debug(f"Cleaned up confirmation {confirmation_id}")

def handle_confirmation_response(confirmation_id: str, response: Dict) -> None:
    """Handle incoming confirmation response from RTVI"""
    with _confirmations_lock:
        if confirmation_id in _pending_confirmations:
            future = _pending_confirmations[confirmation_id]
            if not future.done():
                future.set_result(response)
                logger.debug(f"Set confirmation response for {confirmation_id}: {response}")
            else:
                logger.warning(f"Confirmation {confirmation_id} already completed")
        else:
            logger.warning(f"Received response for unknown confirmation: {confirmation_id}")

class LLMSpyProcessor(FrameProcessor):
    """
    Lightweight frame processor for intercepting LLM conversation events.
    Responsibilities:
    1. Intercepts function call frames and emits RTVI events
    2. Handles OpenTelemetry tracing for function calls
    3. Processes text frames with markdown sanitization
    """

    def __init__(self, rtvi: RTVIProcessor, session_id: str, name: str = "LLMSpyProcessor"):
        super().__init__(name=name)
        self._rtvi = rtvi
        self._session_id = session_id

        # Register this RTVI processor globally for function confirmations
        set_rtvi_processor(rtvi)

        # Tracing setup
        self._tracer = trace.get_tracer("pipecat.tools") if config.ENABLE_TRACING else None
        self._active_spans: Dict[str, Any] = {}  # tool_call_id -> span

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Emit RTVI server messages for function call frames."""
        await super().process_frame(frame, direction)

        if isinstance(frame, TextFrame):
            if config.SANITIZE_TEXT_FOR_TTS:
                await self.push_frame(TextFrame(text=sanitize_markdown(frame.text)), direction)
            else:
                await self.push_frame(frame, direction)
        elif isinstance(frame, FunctionCallInProgressFrame):
            # Start tracing span
            if self._tracer:
                span = self._tracer.start_span(f"Tool: {frame.function_name}", kind=trace.SpanKind.CLIENT)
                span.set_attributes({
                    "tool.name": frame.function_name,
                    "tool.args_json": json.dumps(frame.arguments, default=str)[:4096]
                })
                self._active_spans[frame.tool_call_id] = span

            logger.debug(f"Function call started: {frame.function_name} with args: {frame.arguments}")
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
            await self.push_frame(frame, direction)
        elif isinstance(frame, FunctionCallResultFrame):
            # End tracing span
            if self._tracer and frame.tool_call_id in self._active_spans:
                span = self._active_spans.pop(frame.tool_call_id)
                span.set_attribute("tool.result", json.dumps(frame.result, default=str)[:4096])
                span.end()
            
            logger.debug(f"Function call result: {frame.function_name} with result: {frame.result}")
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
        else:
            await self.push_frame(frame, direction)

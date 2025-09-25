"""
LLM Spy Processor for intercepting function calls and conversation events.
Lightweight frame processor that delegates business logic to ConversationManager.
"""

import asyncio
import json
import time
from typing import Any, Dict, Optional

from opentelemetry import trace
from pipecat.frames.frames import (
    Frame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TextFrame,
    UserStartedSpeakingFrame,
    TTSSpeakFrame
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIServerMessageFrame

from app.agents.voice.automatic.features.charts.chart_tools import (
    reset_chart_turn_count,
)
from app.agents.voice.automatic.features.charts.rtvi.rtvi import emit_chart_components
from app.agents.voice.automatic.rtvi.rtvi import emit_rtvi_event
from app.agents.voice.automatic.utils.conversation_manager import (
    get_conversation_manager,
)
from app.core import config
from app.core.logger import logger

from ..features.text_sanitizer.tts_sanitizer import sanitize_markdown
from app.agents.voice.automatic.audio.audio_manager import get_audio_manager

def _stop_audio_immediately(context: str = "unknown") -> bool:
    """INSTANT audio stopping using simplified AudioManager API."""
    audio_manager = get_audio_manager()
    if audio_manager and (audio_manager.user_has_input or audio_manager.is_playing):
        # Use the simplified stop method
        asyncio.create_task(audio_manager.stop_and_disable_audio())
        
        logger.info(f"INSTANT AUDIO STOP: {context}")
        return True
    return False

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


async def wait_for_confirmation_response(
    confirmation_id: str, timeout_seconds: int = 30
) -> Optional[Dict]:
    """Wait for confirmation response via RTVI events"""
    with _confirmations_lock:
        if confirmation_id not in _pending_confirmations:
            logger.error(f"No pending confirmation found for ID: {confirmation_id}")
            return None
        future = _pending_confirmations[confirmation_id]

    try:
        logger.debug(
            f"Waiting for confirmation response {confirmation_id} with timeout {timeout_seconds}s"
        )
        # Wait for the response with timeout
        response = await asyncio.wait_for(future, timeout=timeout_seconds)
        logger.debug(
            f"Received confirmation response for {confirmation_id}: {response}"
        )
        return response
    except asyncio.TimeoutError:
        logger.warning(
            f"Confirmation timeout for {confirmation_id} after {timeout_seconds}s"
        )
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
                logger.debug(
                    f"Set confirmation response for {confirmation_id}: {response}"
                )
            else:
                logger.warning(f"Confirmation {confirmation_id} already completed")
        else:
            logger.warning(
                f"Received response for unknown confirmation: {confirmation_id}"
            )


class LLMSpyProcessor(FrameProcessor):
    """
    Lightweight frame processor for intercepting LLM conversation events.

    Responsibilities:
    1. Intercepts function call frames and emits RTVI events
    2. Collects LLM responses and delegates to ConversationManager
    3. Handles chart component emission
    4. Processes highlight text for timing correlation
    """

    def __init__(
        self,
        rtvi: RTVIProcessor,
        session_id: str,
        enable_charts: bool,
        name: str = "LLMSpyProcessor",
    ):
        super().__init__(name=name)
        self._rtvi = rtvi
        self._session_id = session_id
        self._enable_charts = enable_charts

        # Register this RTVI processor globally for function confirmations
        set_rtvi_processor(rtvi)

        # Tracing setup
        self._tracer = (
            trace.get_tracer("pipecat.tools") if config.ENABLE_TRACING else None
        )
        self._active_spans: Dict[str, Any] = {}  # tool_call_id -> span

        if self._enable_charts:
            # LLM response collection
            self._accumulated_text = ""
            self._is_collecting_response = False

            # Conversation management (delegates to service)
            self._conversation_manager = get_conversation_manager()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames and delegate conversation logic to ConversationManager."""
        await super().process_frame(frame, direction)

        # TextFrame - INSTANT AUDIO INTERRUPTION (highest priority)
        if isinstance(frame, TextFrame):
            # INSTANT STOP: Any text output means immediate audio interruption
            if frame.text.strip():  # Only stop for non-empty text
                if _stop_audio_immediately("TextFrame - Bot Speaking"):
                    audio_manager = get_audio_manager()
                    if audio_manager:
                        audio_manager.set_bot_speaking(True)
                        logger.info("INSTANT STOP: TextFrame detected - audio interrupted immediately")
            
            if config.SANITIZE_TEXT_FOR_TTS:
                await self.push_frame(
                    TextFrame(text=sanitize_markdown(frame.text)), direction
                )
            else:
                await self.push_frame(frame, direction)

        elif isinstance(frame, UserStartedSpeakingFrame) and self._enable_charts:
            reset_chart_turn_count(self._session_id)
            await self.push_frame(frame, direction)

        # LLM Response Start - begin collecting text and start conversation turn and PREPARE FOR INSTANT STOP (but don't stop yet)
        elif isinstance(frame, LLMFullResponseStartFrame):
            # logger.debug(f"🤖 LLM processing started - audio continues, preparing for instant stop on text output")
            self._is_collecting_response = True
            self._accumulated_text = ""

            # Start conversation turn via ConversationManager
            event = await self._conversation_manager.start_turn_with_events(
                self._session_id
         )
            if event:
                await emit_rtvi_event(self._rtvi, event, self._session_id)
            await self.push_frame(frame, direction)

        # LLM Output - accumulate streaming text & INSTANT AUDIO INTERRUPTION (zero-delay)
        elif isinstance(frame, LLMTextFrame):
            # INSTANT STOP: Any LLM text output triggers immediate audio stop
            if frame.text.strip():  # Only stop for non-empty text
                _stop_audio_immediately("LLMTextFrame - LLM Output")
            
            if self._is_collecting_response and self._enable_charts:
                self._accumulated_text += frame.text
            
            await self.push_frame(frame, direction)

        # LLM Response Complete - send to ConversationManager and FINAL AUDIO STOP CONFIRMATION
        elif isinstance(frame, LLMFullResponseEndFrame) and self._enable_charts:
            # Check if there was actual text output BEFORE clearing it
            has_text_output = self._accumulated_text.strip()
            
            if has_text_output:
                _stop_audio_immediately("Response Complete - Final Stop")
                # logger.info("FINAL STOP: LLM response complete with text output - audio fully stopped")
                
                # Send the response to conversation manager
                event = await self._conversation_manager.add_llm_response_with_events(
                    self._session_id, has_text_output
                )
                if event:
                    await emit_rtvi_event(self._rtvi, event, self._session_id)

            self._accumulated_text = ""
            self._is_collecting_response = False
            
            # Handle case where no text was produced
            audio_manager = get_audio_manager()
            if audio_manager:
                audio_manager.set_bot_speaking(False)
            
            await self.push_frame(frame, direction)
            # logger.debug(f"🤖 LLM response ended - bot marked as not speaking")

        # Function Call Start - emit RTVI event and track in conversation, NO ACTION (let audio continue)
        elif isinstance(frame, FunctionCallInProgressFrame):
            if self._tracer:
                span = self._tracer.start_span(
                    f"Tool: {frame.function_name}", kind=trace.SpanKind.CLIENT
                )
                span.set_attributes(
                    {
                        "tool.name": frame.function_name,
                        "tool.args_json": json.dumps(frame.arguments, default=str)[
                            :4096
                        ],
                    }
                )
                self._active_spans[frame.tool_call_id] = span

            logger.debug(
                f"Function call started: {frame.function_name} with args: {frame.arguments}"
            )
            await self._rtvi.push_frame(
                RTVIServerMessageFrame(
                    data={
                        "type": "tool-call-start",
                        "payload": {
                            "toolCallId": frame.tool_call_id,
                            "functionName": frame.function_name,
                            "arguments": frame.arguments,
                            "timestamp": int(time.time() * 1000),
                        },
                    }
                )
            )
            await self.push_frame(frame, direction)

            # Track in conversation via ConversationManager
            if self._enable_charts:
                event = await self._conversation_manager.add_tool_call_with_events(
                    self._session_id,
                    frame.function_name,
                    frame.arguments,
                    frame.tool_call_id,
                )
                if event:
                    await emit_rtvi_event(self._rtvi, event, self._session_id)

        # Function Call Result - emit RTVI event and track in conversation and NO ACTION (let audio continue)
        elif isinstance(frame, FunctionCallResultFrame):
            # Emit tool-call-result event
            if self._tracer and frame.tool_call_id in self._active_spans:
                span = self._active_spans.pop(frame.tool_call_id)
                span.set_attribute(
                    "tool.result", json.dumps(frame.result, default=str)[:4096]
                )
                span.end()

            logger.debug(
                f"Function call result: {frame.function_name} with result: {frame.result}"
            )
            await self._rtvi.push_frame(
                RTVIServerMessageFrame(
                    data={
                        "type": "tool-call-result",
                        "payload": {
                            "toolCallId": frame.tool_call_id,
                            "functionName": frame.function_name,
                            "arguments": frame.arguments,
                            "result": frame.result,
                            "timestamp": int(time.time() * 1000),
                        },
                    }
                )
            )
            await self.push_frame(frame, direction)

            if self._enable_charts:
                # Track in conversation via ConversationManager (may complete turn)
                events = await self._conversation_manager.add_tool_result_with_events(
                    self._session_id,
                    frame.tool_call_id,
                    frame.function_name,
                    frame.result,
                )
                for event in events:
                    await emit_rtvi_event(self._rtvi, event, self._session_id)

                # Handle chart component emission (works for both local and MCP tools)
                # Always check for pending components after any function call
                await emit_chart_components(
                    self._rtvi, frame.function_name, self._session_id
                )
        else:
            await self.push_frame(frame, direction)

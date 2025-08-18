"""
LLM Spy Processor for intercepting function calls and conversation events.
Lightweight frame processor that delegates business logic to ConversationManager.
"""

import time

from pipecat.frames.frames import (
    Frame, 
    FunctionCallInProgressFrame, 
    FunctionCallResultFrame, 
    LLMTextFrame,
    LLMFullResponseStartFrame, 
    LLMFullResponseEndFrame
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIServerMessageFrame

from app.core.logger import logger
from app.agents.voice.automatic.conversation_manager import get_conversation_manager


class LLMSpyProcessor(FrameProcessor):
    """
    Lightweight frame processor for intercepting LLM conversation events.
    
    Responsibilities:
    1. Intercepts function call frames and emits RTVI events
    2. Collects LLM responses and delegates to ConversationManager
    3. Handles chart component emission
    4. Processes highlight text for timing correlation
    """

    def __init__(self, rtvi: RTVIProcessor, session_id: str, name: str = "LLMSpyProcessor"):
        super().__init__(name=name)
        self._rtvi = rtvi
        self._session_id = session_id
        
        # LLM response collection
        self._accumulated_text = ""
        self._is_collecting_response = False
        
        # Conversation management (delegates to service)
        self._conversation_manager = get_conversation_manager()
        

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames and delegate conversation logic to ConversationManager."""
        await super().process_frame(frame, direction)
        
        # LLM Response Start - begin collecting text and start conversation turn
        if isinstance(frame, LLMFullResponseStartFrame):
            self._is_collecting_response = True
            self._accumulated_text = ""
            
            # Start conversation turn via ConversationManager
            event = await self._conversation_manager.start_turn_with_events(self._session_id)
            if event:
                await self._emit_rtvi_event(event)
                
        # LLM Output - accumulate streaming text
        elif isinstance(frame, LLMTextFrame) and self._is_collecting_response:
            self._accumulated_text += frame.text
            
        # LLM Response Complete - send to ConversationManager
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._accumulated_text.strip():
                event = await self._conversation_manager.add_llm_response_with_events(
                    self._session_id, self._accumulated_text.strip()
                )
                if event:
                    await self._emit_rtvi_event(event)
                    
            self._accumulated_text = ""
            self._is_collecting_response = False

        # Function Call Start - emit RTVI event and track in conversation
        elif isinstance(frame, FunctionCallInProgressFrame):
            # Emit tool-call-start event
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
            
            # Track in conversation via ConversationManager
            event = await self._conversation_manager.add_tool_call_with_events(
                self._session_id, frame.function_name, frame.arguments, frame.tool_call_id
            )
            if event:
                await self._emit_rtvi_event(event)
            
        # Function Call Result - emit RTVI event and track in conversation
        elif isinstance(frame, FunctionCallResultFrame):
            # Emit tool-call-result event
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
            
            # Track in conversation via ConversationManager (may complete turn)
            events = await self._conversation_manager.add_tool_result_with_events(
                self._session_id, frame.tool_call_id, frame.function_name, frame.result
            )
            for event in events:
                await self._emit_rtvi_event(event)
            
            # Handle chart component emission (works for both local and MCP tools)
            # Always check for pending components after any function call
            await self._emit_chart_components(frame.function_name)

        await self.push_frame(frame, direction)
    
    async def _emit_rtvi_event(self, event) -> None:
        """Emit conversation event via RTVI."""
        try:
            await self._rtvi.push_frame(
                RTVIServerMessageFrame(
                    data={
                        "type": event.type,
                        "payload": event.payload
                    }
                )
            )
        except Exception as e:
            logger.error(f"Error emitting RTVI event for session {self._session_id}: {e}")

    async def _emit_chart_components(self, function_name: str) -> None:
        """Emit chart components via RTVI frames after function calls."""
        del function_name  # Unused parameter
        try:
            from app.tools.providers.system.chart_tools import get_pending_chart_emissions
            
            pending_charts = get_pending_chart_emissions(self._session_id)
            
            for chart_data in pending_charts:
                await self._rtvi.push_frame(
                    RTVIServerMessageFrame(
                        data={
                            "type": "ui-component",
                            "payload": chart_data
                        }
                    )
                )
                
        except ImportError:
            pass
        except Exception as e:
            logger.error(f"Error emitting chart components for session {self._session_id}: {e}")


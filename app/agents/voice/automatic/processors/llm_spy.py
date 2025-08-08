import time

from app.core.logger import logger
from pipecat.frames.frames import Frame, FunctionCallInProgressFrame, FunctionCallResultFrame, LLMTextFrame, LLMFullResponseEndFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIServerMessageFrame


# Custom LLMSpyProcessor for streaming function call events
class LLMSpyProcessor(FrameProcessor):
    """Intercepts function call frames to emit RTVI server messages for start and result."""

    def __init__(self, rtvi: RTVIProcessor, name: str = "LLMSpyProcessor"):
        super().__init__(name=name)
        self._rtvi = rtvi
        self._accumulated_text = ""
        self._session_id = "chart_session"  # TODO: Extract actual session ID

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Emit RTVI server messages for function call frames."""
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMTextFrame):
            # Accumulate LLM text for highlight extraction
            self._accumulated_text += frame.text
            logger.debug(f"[{self._session_id}] Accumulated LLM text: '{self._accumulated_text}'")
            
        elif isinstance(frame, FunctionCallInProgressFrame):
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
            
            # Check if this was a chart generation function and emit chart components
            if frame.function_name in ["generate_bar_chart", "generate_line_chart", "generate_donut_chart"]:
                await self._emit_chart_components(frame.function_name)
        
        elif isinstance(frame, LLMFullResponseEndFrame):
            # LLM response is complete, extract highlights from accumulated text
            await self._emit_highlights_for_accumulated_text()
            # Reset accumulated text for next response
            self._accumulated_text = ""

        await self.push_frame(frame, direction)

    async def _emit_chart_components(self, function_name: str):
        """Emit chart components via RTVI frames after chart generation functions"""
        try:
            from app.tools.providers.system.chart_tools import get_pending_chart_emissions
            
            # Get session ID - this might need to be passed differently based on context
            # For now, using a placeholder that can be replaced with proper session extraction
            session_id = "chart_session"  # TODO: Extract actual session ID from context
            
            pending_charts = get_pending_chart_emissions(session_id)
            
            for chart_data in pending_charts:
                logger.info(f"[{session_id}] 🚀 Emitting chart component via RTVI: {chart_data['componentId']}")
                
                await self._rtvi.push_frame(
                    RTVIServerMessageFrame(
                        data={
                            "type": "ui-component",
                            "payload": chart_data
                        }
                    )
                )
                
                logger.info(f"[{session_id}] ✅ Successfully emitted chart component: {chart_data['componentType']}")
                
        except Exception as e:
            logger.error(f"Error emitting chart components: {e}")

    async def _emit_highlights_for_accumulated_text(self):
        """Extract highlights from accumulated LLM text and emit via RTVI"""
        if not self._accumulated_text.strip():
            return
            
        try:
            from app.tools.providers.system.chart_tools import get_latest_chart_context, extract_highlights_from_text
            
            # Get the latest chart context for this session
            chart_context = get_latest_chart_context(self._session_id)
            
            if not chart_context:
                logger.debug(f"[{self._session_id}] No chart context available for highlight extraction")
                return
            
            # Extract highlights from the accumulated text
            highlights = extract_highlights_from_text(self._accumulated_text, chart_context)
            
            if highlights:
                logger.info(f"[{self._session_id}] 🎯 Extracted {len(highlights)} highlights from LLM response")
                
                # Emit bot transcript with highlights
                await self._rtvi.push_frame(
                    RTVIServerMessageFrame(
                        data={
                            "type": "bot-transcript",
                            "payload": {
                                "text": self._accumulated_text,
                                "highlights": highlights,
                                "timestamp": int(time.time() * 1000)
                            }
                        }
                    )
                )
                
                logger.info(f"[{self._session_id}] ✅ Emitted bot transcript with highlights: {[h['text'] for h in highlights]}")
            else:
                logger.debug(f"[{self._session_id}] No highlights found in text: '{self._accumulated_text[:100]}...'")
                
        except Exception as e:
            logger.error(f"[{self._session_id}] Error extracting highlights: {e}")
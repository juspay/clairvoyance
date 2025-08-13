import time

from app.core.logger import logger
from pipecat.frames.frames import Frame, FunctionCallInProgressFrame, FunctionCallResultFrame, LLMMessagesFrame, LLMTextFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIServerMessageFrame


# Custom LLMSpyProcessor for streaming function call events
class LLMSpyProcessor(FrameProcessor):
    """Intercepts LLM and function call frames to log conversation flow and emit RTVI server messages."""

    def __init__(self, rtvi: RTVIProcessor, name: str = "LLMSpyProcessor"):
        super().__init__(name=name)
        self._rtvi = rtvi
        self._accumulated_text = ""  # Store accumulated LLM response text
        self._is_collecting_response = False  # Track if we're between start and end frames

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Log LLM conversation flow and emit RTVI server messages for function call frames."""
        await super().process_frame(frame, direction)

        # LLM Input (Messages going to LLM)
        if isinstance(frame, LLMMessagesFrame):
            if frame.messages and len(frame.messages) > 0:
                last_message = frame.messages[-1]
                if last_message.get('role') == 'user':
                    logger.info(f"💭 LLM Input: '{last_message.get('content', '')}'")

        # LLM Response Start - begin collecting text
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._is_collecting_response = True
            self._accumulated_text = ""
            
        # LLM Output (Response from LLM) - accumulate streaming text only during response
        elif isinstance(frame, LLMTextFrame) and self._is_collecting_response:
            self._accumulated_text += frame.text
            
        # LLM Response Complete - log the full accumulated response
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._accumulated_text.strip():
                logger.info(f"🧠 LLM Output: '{self._accumulated_text.strip()}'")
            self._accumulated_text = ""
            self._is_collecting_response = False

        # Tool Events
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

        await self.push_frame(frame, direction)
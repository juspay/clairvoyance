import time

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

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Emit RTVI server messages for function call frames."""
        await super().process_frame(frame, direction)

        if isinstance(frame, FunctionCallInProgressFrame):
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
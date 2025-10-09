"""
Text Capture Processor

Frame processor for capturing LLM text output and managing conversation flow.
Similar to voice agent's LLMSpyProcessor but designed for text-only responses.
"""

from pipecat.frames.frames import (
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.core.logger import logger


class TextCaptureProcessor(FrameProcessor):
    """
    Captures LLM text output and manages conversation state.

    This processor follows the same pattern as the voice agent's LLMSpyProcessor
    but is specialized for text-only output without audio/TTS complexity.
    """

    def __init__(self, session_id: str, cache_manager, response_collector):
        super().__init__()
        self.session_id = session_id
        self.cache_manager = cache_manager
        self.collector = response_collector
        self.accumulated_response = ""

    async def process_frame(self, frame, direction: FrameDirection):
        """Process frames following the voice agent's LLMSpyProcessor pattern."""
        # Follow the exact pattern from voice agent's LLMSpyProcessor
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMTextFrame):
            logger.info(f"Captured LLM text: '{frame.text}'")
            # Accumulate response text
            self.accumulated_response += frame.text
            # Add to collector for streaming
            self.collector.text_chunks.append(frame.text)

            # Put chunk in queue for immediate streaming (exactly as LLM gives us)
            await self.collector.chunk_queue.put(frame.text)
            logger.info(f"Added chunk to streaming queue: '{frame.text}'")

            # Continue frame flow
            await self.push_frame(frame, direction)

        elif isinstance(frame, LLMFullResponseEndFrame):
            logger.info("LLM response complete")
            # Save the complete response to conversation history via cache manager
            if self.accumulated_response:
                history = await self.cache_manager.load_conversation_history(
                    self.session_id
                )

                # Clean the accumulated response (remove extra spaces)
                clean_response = self.accumulated_response.strip()

                # Check if this exact response already exists to prevent duplicates
                response_exists = False
                for msg in reversed(history):
                    if (
                        msg.get("role") == "assistant"
                        and msg.get("content", "").strip() == clean_response
                    ):
                        response_exists = True
                        logger.info(
                            "Assistant response already exists in history, skipping duplicate"
                        )
                        break

                # Only add if it's not a duplicate
                if not response_exists and clean_response:
                    # Filter out tool calls and only store clean user/assistant messages
                    clean_history = []
                    for msg in history:
                        # Only keep user and assistant messages, skip tool calls
                        if msg.get("role") in ["user", "assistant"] and not msg.get(
                            "tool_calls"
                        ):
                            clean_history.append(msg)

                    clean_history.append(
                        {"role": "assistant", "content": clean_response}
                    )
                    await self.cache_manager.update_conversation_history(
                        self.session_id, clean_history
                    )
                    logger.info(
                        f"Saved clean assistant response to conversation history: '{clean_response}'"
                    )

                # Set complete response and signal completion
                self.collector.complete_response = clean_response
                self.collector.is_complete = True

                # Put sentinel value in queue to signal end of streaming
                await self.collector.chunk_queue.put(None)
                logger.info("Added end-of-stream sentinel to queue")

                self.collector.complete_event.set()
                # Reset for next response
                self.accumulated_response = ""

            # Continue frame flow
            await self.push_frame(frame, direction)

        elif isinstance(frame, FunctionCallInProgressFrame):
            # Just log function calls like voice agent's LLMSpyProcessor
            # Don't execute them - let the LLM service handle execution automatically
            logger.info(
                f"Function call started: {frame.function_name} with args: {frame.arguments}"
            )
            await self.push_frame(frame, direction)

        elif isinstance(frame, FunctionCallResultFrame):
            # Log function call results like voice agent's LLMSpyProcessor
            logger.info(
                f"Function call result: {frame.function_name} with result: {frame.result}"
            )
            await self.push_frame(frame, direction)
        else:
            # For all other frames, just continue the flow
            await self.push_frame(frame, direction)

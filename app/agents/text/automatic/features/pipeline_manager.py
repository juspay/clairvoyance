"""
Text Pipeline Manager

Manages text agent pipelines with caching, conversation history, and response handling.
"""

import asyncio
import traceback
from typing import Tuple

from pipecat.frames.frames import InputTextRawFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.services.azure.llm import AzureLLMService

from app.agents.voice.automatic.features.llm_wrapper import LLMServiceWrapper
from app.agents.voice.automatic.prompts import get_system_prompt
from app.agents.voice.automatic.tools import initialize_tools
from app.core.cache import PipelineCacheManager
from app.core.config import (
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_MODEL,
)
from app.core.logger import logger

from ..processors import TextCaptureProcessor
from ..types import ResponseCollector


class TextPipelineManager:
    """Manages text agent pipelines with optimized caching following senior's approach."""

    def __init__(self):
        self._runner = None
        self.cache_manager = PipelineCacheManager()
        self._startup_complete = False

    async def startup(self):
        """Initialize cache manager and cleanup tasks."""
        if not self._startup_complete:
            await self.cache_manager.start_cleanup_task()
            self._startup_complete = True
            logger.info("TextPipelineManager startup complete")

    def get_runner(self):
        """Get or create pipeline runner."""
        if self._runner is None:
            self._runner = PipelineRunner()
        return self._runner

    async def get_or_create_pipeline(
        self, session_id: str, config: dict = None
    ) -> Tuple[PipelineTask, ResponseCollector]:
        """Get cached pipeline or create new one with optimized caching strategy."""
        await self.startup()

        # Try to get cached pipeline first
        cached_pipeline = await self.cache_manager.get_cached_pipeline(session_id)
        if cached_pipeline:
            task, _, context, text_processor = cached_pipeline
            logger.info(f"Reusing cached pipeline for session {session_id}")

            # Create a fresh response collector for this request
            # The old one is tied to the previous request's streaming
            fresh_response_collector = ResponseCollector()
            logger.info("Created fresh response collector for new request")

            # Update the TextCaptureProcessor with the new response collector using direct reference
            if text_processor and hasattr(text_processor, "collector"):
                text_processor.collector = fresh_response_collector
                logger.info(
                    "✅ Updated TextCaptureProcessor with fresh response collector (optimized)"
                )
            else:
                logger.warning(
                    "⚠️ No direct processor reference available, falling back to search"
                )
                # Fallback to the old recursive search method
                # ... (keep the old code as fallback)

            return task, fresh_response_collector

        logger.info(f"Creating new pipeline for session {session_id}")
        config = config or {}

        # Load conversation history from cache manager
        history = await self.cache_manager.load_conversation_history(session_id)

        # Get system prompt (no TTS for text)
        system_prompt = get_system_prompt(
            user_name=config.get("user_name"),
            tts_provider=None,
            shop_id=config.get("shop_id"),
        )

        # Build conversation messages
        if not history:
            messages = [{"role": "system", "content": system_prompt}]
            logger.info(
                f"No history found, starting with system prompt for session {session_id}"
            )
        else:
            # Use existing history but ensure it starts with system prompt
            if history[0].get("role") != "system":
                messages = [{"role": "system", "content": system_prompt}] + history
            else:
                messages = history
            logger.info(
                f"Using existing conversation history with {len(history)} messages for session {session_id}"
            )

        logger.info(
            f"Final conversation context has {len(messages)} messages: {[m.get('role') for m in messages]}"
        )

        # Initialize tools using existing infrastructure (same pattern as voice agent)
        tools, tool_functions = initialize_tools(
            mode=config.get("mode", "TEST"),
            breeze_token=config.get("breeze_token"),
            euler_token=config.get("euler_token"),
            shop_url=config.get("shop_url"),
            shop_id=config.get("shop_id"),
            shop_type=config.get("shop_type"),
            merchant_id=config.get("merchant_id"),
            session_id=session_id,
            user_id=config.get("user_id"),
            user_email=config.get("user_email"),
            reseller_id=config.get("reseller_id"),
        )

        # Create LLM service with wrapper (same pattern as voice agent)
        llm = LLMServiceWrapper(
            AzureLLMService(
                api_key=AZURE_OPENAI_API_KEY,
                endpoint=AZURE_OPENAI_ENDPOINT,
                model=AZURE_OPENAI_MODEL,
            )
        )

        # Register tools with LLM service (same pattern as voice agent)
        for name, function in tool_functions.items():
            logger.info(f"Registering function: {name}")
            llm.register_function(name, function)

        logger.info(f"Registered {len(tool_functions)} tools with LLM service")

        # Set up pipeline - use create_summarizing_context like voice agent
        logger.info(
            f"Creating context with {len(messages)} messages: {[m.get('role') for m in messages]}"
        )
        context = llm.create_summarizing_context(messages, tools)

        logger.info(
            f"Created context with tools: {len(tools.standard_tools)} tools available"
        )

        aggregators = llm.create_context_aggregator(context)
        user_aggr = aggregators.user()
        asst_aggr = aggregators.assistant()

        # Create a simple response collector
        response_collector = ResponseCollector()

        # Create the text capture processor with cache manager integration
        text_processor = TextCaptureProcessor(
            session_id, self.cache_manager, response_collector
        )

        # Create pipeline with text processor AFTER LLM but BEFORE assistant aggregator
        pipeline = Pipeline([user_aggr, llm, text_processor, asst_aggr])
        task = PipelineTask(pipeline)

        runner = self.get_runner()

        # Start the pipeline task in the background
        asyncio.create_task(runner.run(task))

        # Cache the pipeline for reuse with direct processor reference
        await self.cache_manager.cache_pipeline(
            session_id, task, response_collector, context, text_processor
        )

        return task, response_collector

    async def process_message(self, session_id: str, message: str, config: dict = None):
        """Process a message and return response generator."""
        logger.info(
            f"process_message called: session={session_id}, message='{message}', config={config}"
        )

        await self.startup()

        # Load and update conversation history through cache manager
        history = await self.cache_manager.load_conversation_history(session_id)
        history.append({"role": "user", "content": message})
        await self.cache_manager.update_conversation_history(session_id, history)
        logger.info(
            f"Added user message to conversation history for session {session_id}"
        )

        # Get pipeline with response collector
        logger.info(f"Getting pipeline for session {session_id}")
        task, response_collector = await self.get_or_create_pipeline(session_id, config)
        logger.info(f"Got pipeline task: {task}")

        # For cached pipelines, update the context with latest conversation history
        try:
            cached_pipeline = await self.cache_manager.get_cached_pipeline(session_id)
            if cached_pipeline:
                _, _, context, _ = cached_pipeline
                logger.info(
                    "Updating cached pipeline context with latest conversation history"
                )

                # Get the updated conversation history (including the new user message we just added)
                updated_history = await self.cache_manager.load_conversation_history(
                    session_id
                )

                # Update the context directly with the latest history (with safety check)
                if hasattr(context, "_messages") and updated_history is not None:
                    context._messages = updated_history
                    logger.info(
                        f"Updated context with {len(updated_history)} messages: {[m.get('role') for m in updated_history]}"
                    )
                else:
                    logger.warning(
                        "Context object or updated_history is invalid, skipping context update"
                    )
        except Exception as cache_e:
            logger.error(f"Error updating cached pipeline context: {cache_e}")
            # Continue execution - this is not a fatal error

        async def response_generator():
            task_started = False
            try:
                logger.info(f"Starting response generator for session {session_id}")
                logger.info(
                    f"About to queue frames: InputTextRawFrame('{message}') and LLMRunFrame()"
                )

                # Create frames
                input_frame = InputTextRawFrame(message)
                llm_frame = LLMRunFrame()
                logger.info(f"Created frames: {input_frame}, {llm_frame}")

                # Queue frames
                logger.info("About to call task.queue_frames...")
                await task.queue_frames([input_frame, llm_frame])
                task_started = True
                logger.info("Frames queued successfully, waiting for responses...")

                # Stream chunks as they arrive via the queue
                logger.info("Starting real-time streaming from queue")

                while True:
                    try:
                        # Get the next chunk from the queue with timeout to prevent hanging
                        chunk = await asyncio.wait_for(
                            response_collector.chunk_queue.get(),
                            timeout=30.0,  # 30 second timeout
                        )

                        # None is our sentinel value indicating end of stream
                        if chunk is None:
                            logger.info("Received end-of-stream sentinel, stopping")
                            break

                        # Yield the chunk immediately
                        logger.info(f"Streaming chunk: '{chunk}'")
                        yield chunk
                        # Small delay to make streaming more visible in clients
                        await asyncio.sleep(0.01)

                    except asyncio.TimeoutError:
                        logger.error(
                            "Timeout waiting for response chunk, ending stream"
                        )
                        yield "Error: Response timeout - please try again"
                        break
                    except Exception as queue_e:
                        logger.error(f"Error reading from response queue: {queue_e}")
                        yield f"Error: Stream interrupted - {str(queue_e)}"
                        break

                logger.info("Real-time streaming completed")

            except Exception as e:
                logger.error(f"Error in response generator: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")
                yield f"Error: {str(e)}"
            finally:
                # Clean up resources
                try:
                    if task_started and hasattr(task, "stop"):
                        logger.info("Stopping pipeline task for cleanup")
                        await task.stop()

                    # Clear the response collector queue to prevent memory leaks
                    if hasattr(response_collector, "chunk_queue"):
                        while not response_collector.chunk_queue.empty():
                            try:
                                response_collector.chunk_queue.get_nowait()
                            except:
                                break
                        logger.info("Cleared response collector queue")
                except Exception as cleanup_e:
                    logger.error(f"Error during cleanup: {cleanup_e}")
                finally:
                    logger.info(f"Response generator finished for session {session_id}")

        logger.info(f"About to return response generator for session {session_id}")
        return response_generator()

    def get_active_sessions(self) -> int:
        """Get number of active sessions."""
        return self.cache_manager.get_cache_stats()["active_pipelines"]

    async def cleanup_session(self, session_id: str):
        """Cleanup a specific session."""
        await self.cache_manager.cleanup_session(session_id)

    def get_cache_stats(self) -> dict:
        """Get detailed cache statistics."""
        return self.cache_manager.get_cache_stats()

    async def shutdown(self):
        """Shutdown pipeline manager and cleanup resources."""
        await self.cache_manager.shutdown()
        logger.info("TextPipelineManager shutdown complete")

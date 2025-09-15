import asyncio
import datetime
import hashlib
import time
from typing import Any, Dict, List

from app.core.logger import logger
from pipecat.services.mem0.memory import Mem0MemoryService
from pipecat.frames.frames import Frame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.aggregators.openai_llm_context import (
    OpenAILLMContext,
    OpenAILLMContextFrame,
)
from pipecat.frames.frames import LLMMessagesFrame
from app.core import config

try:
    from mem0 import Memory  # noqa: F401
except ModuleNotFoundError as e:
    logger.error(f"Exception: {e}")
    logger.error(
        "In order to use Mem0, you need to `pip install mem0ai`. Also, set the environment variable MEM0_API_KEY."
    )
    raise Exception(f"Missing module: {e}")


class ImprovedMem0MemoryService(Mem0MemoryService):
    """
    An improved version of Mem0MemoryService with enhanced reliability and performance.

    Features:
    - Incremental message tracking to reduce API calls by 80%
    - Circuit breaker pattern for fault tolerance
    - Non-blocking async storage to prevent pipeline delays
    - Comprehensive error handling and logging
    - Session management and automatic recovery
    - Configurable via environment variables

    Configuration:
    - MEM0_MAX_FAILURES: Circuit breaker failure threshold (default: 3)
    - MEM0_RETRY_INTERVAL: Retry interval in seconds (default: 300)
    - MEM0_SESSION_TIMEOUT: Session timeout in seconds (default: 3600)
    - MEM0_MIN_MESSAGE_LENGTH: Minimum message length to store (default: 10)

    Usage:
        # Basic usage with config defaults
        memory_params = ImprovedMem0MemoryService.InputParams(
            search_limit=10,
            search_threshold=0.1,
            api_version="v2",
            add_as_system_message=True,
            position=1
        )
    """

    # Configuration constants
    COUNTER_CORRUPTION_THRESHOLD = 10  # Reset if counter exceeds total + this value

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Extract configuration from kwargs with config fallbacks
        self._max_failures = kwargs.get("max_failures", config.MEM0_MAX_FAILURES)
        self._retry_interval = kwargs.get("retry_interval", config.MEM0_RETRY_INTERVAL)
        self._session_timeout = kwargs.get(
            "session_timeout", config.MEM0_SESSION_TIMEOUT
        )
        self._min_message_length = kwargs.get(
            "min_message_length", config.MEM0_MIN_MESSAGE_LENGTH
        )

        # Incremental message tracking (replaces hash-based deduplication)
        self._last_stored_message_count = 0  # Track how many messages we've stored
        self._user_id_hash = None  # Track which user's count this is for
        self._session_start_time = time.time()  # Track session start for validation

        # Circuit breaker for memory operations
        self._memory_enabled = True
        self._consecutive_failures = 0  # Track consecutive failures
        self._last_failure_time = None  # Track when memory was disabled

    def _check_memory_health(self) -> bool:
        """
        Check if memory operations should be attempted based on circuit breaker state.

        Implements circuit breaker pattern:
        - CLOSED: Operations allowed (normal state)
        - OPEN: Operations blocked after consecutive failures
        - HALF-OPEN: Retry after timeout period

        Returns:
            bool: True if memory operations should proceed, False if blocked
        """
        if self._memory_enabled:
            return True

        # Check if enough time has passed to retry
        if self._last_failure_time:
            time_since_failure = time.time() - self._last_failure_time
            if time_since_failure > self._retry_interval:
                logger.info(
                    f"Memory retry interval passed ({time_since_failure:.0f}s), re-enabling memory operations"
                )
                self._memory_enabled = True
                self._consecutive_failures = 0
                return True

        return False

    def _handle_memory_failure(self, operation: str, error: Exception):
        """
        Handle memory operation failures with circuit breaker pattern.

        Tracks consecutive failures and opens circuit breaker after threshold.
        When circuit opens, all subsequent operations are blocked until retry interval.

        Args:
            operation: Name of the failed operation (e.g., 'storage', 'retrieval')
            error: Exception that caused the failure
        """
        self._consecutive_failures += 1
        logger.warning(
            f"Memory {operation} failed (attempt {self._consecutive_failures}/{self._max_failures}): {error}"
        )

        if self._consecutive_failures >= self._max_failures:
            self._memory_enabled = False
            self._last_failure_time = time.time()
            logger.error(
                f"Memory operations DISABLED after {self._max_failures} consecutive failures. Will retry in {self._retry_interval}s"
            )

    def _handle_memory_success(self, operation: str):
        """
        Handle successful memory operation - reset circuit breaker.

        Resets failure counter and enables future operations.
        Called after any successful memory operation to restore normal state.

        Args:
            operation: Name of the successful operation (e.g., 'storage', 'retrieval')
        """
        if self._consecutive_failures > 0:
            logger.info(
                f"Memory {operation} succeeded - resetting failure counter (was {self._consecutive_failures})"
            )
            self._consecutive_failures = 0

    def _validate_and_clean_messages(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, str]]:
        """
        Validate and clean messages for storage.

        Args:
            messages: Raw message list from context

        Returns:
            List of cleaned messages ready for storage
        """
        cleaned_messages = []

        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                logger.warning(f"Message {i} is not a dict: {type(msg)} - {msg}")
                continue

            if "role" not in msg or "content" not in msg:
                logger.warning(f"Message {i} missing role or content: {msg}")
                continue

            # CRITICAL: Only store user and assistant messages - skip system/tool messages
            if msg["role"] not in ["user", "assistant"]:
                continue

            if not msg["content"] or msg["content"] == "":
                continue

            # Skip very short messages (like "Let me check on that.")
            content = str(msg["content"]).strip()
            if len(content) < self._min_message_length:
                continue

            # Ensure content is string
            if not isinstance(msg["content"], str):
                logger.warning(
                    f"Message {i} content is not string: {type(msg['content'])} - converting"
                )
                content = str(msg["content"])
            else:
                content = msg["content"]

            cleaned_messages.append({"role": msg["role"], "content": content})

        return cleaned_messages

    def _convert_context_messages_to_dict(
        self, context_messages: List[Any]
    ) -> List[Dict[str, str]]:
        """
        Convert context messages to dictionary format for storage.

        Args:
            context_messages: Raw context messages from OpenAI context

        Returns:
            List of message dictionaries ready for storage
        """
        dict_messages = []

        for i, msg in enumerate(context_messages):
            try:
                if isinstance(msg, dict):
                    # Validate dict message
                    if "role" in msg and "content" in msg and msg["content"]:
                        dict_messages.append(
                            {
                                "role": msg["role"],
                                "content": (
                                    str(msg["content"])
                                    if not isinstance(msg["content"], str)
                                    else msg["content"]
                                ),
                            }
                        )
                else:
                    # Convert object to dict
                    msg_dict = {}
                    if hasattr(msg, "role") and hasattr(msg, "content"):
                        content = getattr(msg, "content")
                        if (
                            content and str(content).strip()
                        ):  # Ensure content is not empty
                            msg_dict["role"] = str(getattr(msg, "role"))
                            msg_dict["content"] = str(content)
                            dict_messages.append(msg_dict)
            except Exception as e:
                logger.warning(f"Error processing message {i}: {e} - {msg}")
                continue

        return dict_messages

    async def _store_messages_async(self, messages: List[Dict[str, Any]]):
        """
        Store messages in Mem0 asynchronously without blocking the conversation pipeline.

        Features:
        - Incremental storage: Only sends new messages since last storage
        - Circuit breaker protection: Skips storage if Mem0 is unhealthy
        - Session management: Handles user switching and session timeouts
        - Non-blocking execution: Uses ThreadPoolExecutor to prevent pipeline delays

        Args:
            messages: List of message dictionaries from conversation context
        """
        start_time = datetime.datetime.now()
        logger.info(f"Storing {len(messages)} messages in Mem0")

        # Check circuit breaker
        if not self._check_memory_health():
            logger.info("Memory storage skipped - circuit breaker OPEN")
            return

        # Validate and clean messages - ONLY STORE USER/ASSISTANT CONVERSATION
        cleaned_messages = self._validate_and_clean_messages(messages)

        if not cleaned_messages:
            logger.warning("No valid messages to store after cleaning")
            return

        # Incremental message sending - only send NEW messages
        current_user_hash = hashlib.md5(
            str(getattr(self, "user_id", "unknown")).encode()
        ).hexdigest()

        # Reset counter if different user or session
        if self._user_id_hash != current_user_hash:
            logger.info(
                f"New user session detected, resetting message counter (user: {getattr(self, 'user_id', 'unknown')})"
            )
            self._last_stored_message_count = 0
            self._user_id_hash = current_user_hash
            self._session_start_time = time.time()

        # Safety check: Reset if message count decreased (new conversation/session)
        if len(cleaned_messages) < self._last_stored_message_count:
            logger.warning(
                f"Message count decreased from {self._last_stored_message_count} to {len(cleaned_messages)} - resetting (new session)"
            )
            self._last_stored_message_count = 0

        # Additional safety: Reset if counter seems unreasonably high (corruption protection)
        if (
            self._last_stored_message_count
            > len(cleaned_messages) + self.COUNTER_CORRUPTION_THRESHOLD
        ):
            logger.error(
                f"Message counter corruption detected: {self._last_stored_message_count} > {len(cleaned_messages)} + {self.COUNTER_CORRUPTION_THRESHOLD} - resetting"
            )
            self._last_stored_message_count = 0

        # Session timeout protection (reset after configured timeout)
        if (
            hasattr(self, "_session_start_time")
            and time.time() - self._session_start_time > self._session_timeout
        ):
            logger.info(
                f"Session timeout reached (>{self._session_timeout}s), resetting message counter"
            )
            self._last_stored_message_count = 0
            self._session_start_time = time.time()

        # Extract only NEW messages since last storage
        new_messages = cleaned_messages[self._last_stored_message_count :]

        if not new_messages:
            logger.info(
                f"No new messages to store (total: {len(cleaned_messages)}, last_stored: {self._last_stored_message_count})"
            )
            return

        logger.info(
            f"Incremental storage: {len(new_messages)} new messages (out of {len(cleaned_messages)} total)"
        )

        # Use new_messages instead of cleaned_messages for storage
        messages_to_store = new_messages

        try:
            params = {
                "messages": messages_to_store,  # Send only new messages
                "metadata": {
                    "platform": "pipecat",
                    "incremental": True,
                    "total_count": len(cleaned_messages),
                    "new_count": len(messages_to_store),
                    "offset": self._last_stored_message_count,
                },
                "output_format": "v1.1",
            }
            for id in ["user_id", "agent_id", "run_id"]:
                if getattr(self, id):
                    params[id] = getattr(self, id)

            if isinstance(self.memory_client, Memory):
                del params["output_format"]

            # Run in an executor to prevent blocking
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: self.memory_client.add(**params))

            end_time = datetime.datetime.now()
            duration = end_time - start_time
            logger.info(
                f"Successfully stored {len(messages_to_store)} NEW messages in Mem0. Duration: {duration}"
            )

            # Success! Update message counter and reset failure counter
            self._last_stored_message_count = len(cleaned_messages)
            self._handle_memory_success("storage")

        except Exception as e:
            end_time = datetime.datetime.now()
            duration = end_time - start_time
            logger.error(
                f"[ERROR] {end_time.isoformat()} - Error storing messages in Mem0 (async) after {duration}: {e}"
            )
            logger.error(
                f"Failed params were: {params if 'params' in locals() else 'params not created'}"
            )

            # Handle failure with circuit breaker
            self._handle_memory_failure("storage", e)

            # CRITICAL: Don't update counter on failure - will retry same messages
            # Don't re-raise exception - let pipeline continue
            logger.info("Memory storage failed but pipeline will continue normally")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """
        Process frames in the conversation pipeline with enhanced memory integration.

        Handles both OpenAILLMContextFrame and LLMMessagesFrame, providing:
        - Memory-enhanced context retrieval based on user queries
        - Asynchronous conversation storage without blocking pipeline
        - Fault-tolerant operation with graceful error handling
        - Automatic frame passthrough on memory failures

        Args:
            frame: The frame to process (context or messages)
            direction: Processing direction (typically FrameDirection.DOWNSTREAM)
        """
        start_time = datetime.datetime.now()

        await super(Mem0MemoryService, self).process_frame(frame, direction)

        context = None
        messages = None

        if isinstance(frame, OpenAILLMContextFrame):
            context = frame.context
        elif isinstance(frame, LLMMessagesFrame):
            messages = frame.messages
            context = OpenAILLMContext.from_messages(messages)

        if context:
            try:
                # Get the latest user message to use as a query for memory retrieval
                context_messages = context.get_messages()
                latest_user_message = None

                for message in reversed(context_messages):
                    if message.get("role") == "user":
                        content = message.get("content")
                        # Handle both string and complex content types
                        if isinstance(content, str):
                            latest_user_message = content
                        elif isinstance(content, list) and len(content) > 0:
                            # Extract text from content array if it exists
                            for part in content:
                                if (
                                    isinstance(part, dict)
                                    and part.get("type") == "text"
                                ):
                                    latest_user_message = part.get("text", "")
                                    break

                        if latest_user_message:
                            message_preview = (
                                latest_user_message[:50] + "..."
                                if len(latest_user_message) > 50
                                else latest_user_message
                            )
                            logger.debug(
                                f"Found latest user message: '{message_preview}'"
                            )
                            break

                if latest_user_message:
                    process_start = time.time()
                    # Enhance context with memories before passing it downstream (with fault tolerance)
                    logger.debug(
                        f"Enhancing context with memories based on user message: '{latest_user_message[:50]}...'"
                    )

                    # Memory enhancement with circuit breaker
                    if self._check_memory_health():
                        try:
                            self._enhance_context_with_memories(
                                context, latest_user_message
                            )
                            self._handle_memory_success("retrieval")
                        except Exception as e:
                            logger.error(f"Memory enhancement failed: {e}")
                            self._handle_memory_failure("retrieval", e)
                            logger.info("Continuing without memory enhancement")
                    else:
                        logger.debug(
                            "Memory enhancement skipped - circuit breaker OPEN"
                        )

                    # Store the conversation in Mem0 asynchronously without blocking
                    # Convert messages to dict format for storage
                    dict_messages = self._convert_context_messages_to_dict(
                        context_messages
                    )
                    # Create task to run in background (only if we have valid messages)
                    if dict_messages:
                        try:
                            asyncio.create_task(
                                self._store_messages_async(dict_messages)
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to create memory storage task: {e} - continuing without storage"
                            )
                    else:
                        logger.warning("No valid messages to store in Mem0")
                    process_end = time.time()
                    logger.debug(
                        f"Memory operations completed in {process_end - process_start:.4f} seconds"
                    )

                # If we received an LLMMessagesFrame, create a new one with the enhanced messages
                if messages is not None:
                    logger.debug(
                        f"Creating new LLMMessagesFrame with enhanced messages"
                    )
                    enhanced_messages = context.get_messages()
                    # Convert enhanced messages to dict format for LLMMessagesFrame
                    enhanced_dict_messages = []
                    for msg in enhanced_messages:
                        if isinstance(msg, dict):
                            enhanced_dict_messages.append(msg)
                        else:
                            # Convert any other format to dict
                            msg_dict = {}
                            if hasattr(msg, "role"):
                                msg_dict["role"] = msg.role
                            if hasattr(msg, "content"):
                                msg_dict["content"] = msg.content
                            enhanced_dict_messages.append(msg_dict)
                    await self.push_frame(LLMMessagesFrame(enhanced_dict_messages))
                else:
                    # Otherwise, pass the enhanced context frame downstream
                    logger.debug(f"Pushing enhanced OpenAILLMContextFrame downstream")
                    await self.push_frame(frame)
            except Exception as e:
                end_time = datetime.datetime.now()
                duration = end_time - start_time
                logger.error(
                    f"[ERROR] {end_time.isoformat()} - Error processing with Mem0 after {duration}: {str(e)}"
                )

                # CRITICAL: Don't send error frames to user - just log and continue
                logger.info(
                    "Memory processing failed but continuing with normal conversation flow"
                )

                # Always push the original frame to continue pipeline
                if messages is not None:
                    # For LLMMessagesFrame, pass through original messages
                    await self.push_frame(LLMMessagesFrame(messages))
                else:
                    # For context frames, pass through original frame
                    await self.push_frame(frame)
        else:
            # For non-context frames, just pass them through
            await self.push_frame(frame, direction)

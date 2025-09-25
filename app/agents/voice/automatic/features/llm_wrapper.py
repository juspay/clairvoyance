import asyncio
import uuid
from typing import Any, Callable, Dict, List, Optional

from httpx import HTTPStatusError

from app.agents.voice.automatic.features.hitl.hitl import get_hitl_manager
from app.agents.voice.automatic.features.hitl.utils import is_dangerous_operation
from app.agents.voice.automatic.features.summarizer.context_summarizer import (
    ContextSummarizer,
)
from app.core import config
from app.core.config import (
    HITL_ENABLE,
    TOOL_MAX_RETRIES,
    TOOL_RETRY_ENABLE,
)
from app.core.logger import logger


class LLMServiceWrapper:
    def __init__(self, llm_service):
        logger.debug(
            f"LLM Wrapper: Initializing wrapper for {type(llm_service).__name__}"
        )
        self._llm_service = llm_service
        self._registered_functions = {}

        # Wrap the register_function method to intercept function registrations
        self._original_register_function = getattr(
            llm_service, "register_function", None
        )
        if self._original_register_function:
            logger.debug("LLM Wrapper: Found register_function method, wrapping it")
            llm_service.register_function = self._wrapped_register_function
        else:
            logger.warning(
                "LLM Wrapper: LLM service does not have register_function method - HITL confirmation will not work"
            )
            logger.debug(f"LLM Wrapper: LLM service type: {type(llm_service)}")

    def _wrapped_register_function(self, name: str, function: Callable):
        """Wrap function registration to intercept dangerous operations and add retries."""
        logger.debug(f"LLM Wrapper: Registering function: {name}")
        self._registered_functions[name] = function

        # First, wrap the function with retry logic
        retry_wrapped_function = self._add_retry_wrapper(name, function)

        # Then, if HITL is enabled and the operation is dangerous, wrap it with HITL confirmation
        if HITL_ENABLE and is_dangerous_operation(name):
            logger.debug(
                f"LLM Wrapper: Wrapping dangerous function {name} with HITL confirmation"
            )

            async def hitl_wrapped_function(params):
                """Wrapper that adds confirmation for dangerous operations."""
                try:
                    arguments = getattr(params, "arguments", {})
                    tool_call_id = getattr(params, "tool_call_id", str(uuid.uuid4()))
                    result_callback = getattr(params, "result_callback", None)

                    if not result_callback:
                        logger.error(f"No result_callback found for function {name}")
                        return

                    hitl_manager = get_hitl_manager()
                    try:
                        confirmation_result = await hitl_manager.request_confirmation(
                            function_name=name,
                            arguments=arguments,
                            tool_call_id=tool_call_id,
                        )
                        final_args = confirmation_result.get(
                            "modified_arguments", arguments
                        )
                        logger.info(f"User approved function {name}, executing...")

                        if hasattr(params, "arguments"):
                            params.arguments = final_args

                        # Execute the retry-wrapped function
                        result = await retry_wrapped_function(params)

                        success_msg = hitl_manager.generate_success_message(
                            name, final_args
                        )
                        if hasattr(params, "result_callback") and result_callback:
                            enhanced_result = (
                                f"{result}\n\n{success_msg}"
                                if isinstance(result, str)
                                else f"{str(result)}\n\n{success_msg}"
                            )
                            await result_callback(enhanced_result)

                        return result
                    except Exception as e:
                        logger.error(f"Confirmation process failed for {name}: {e}")
                        if result_callback:
                            await result_callback(
                                {"error": f"Confirmation failed: {str(e)}"}
                            )
                        raise
                except Exception as e:
                    logger.error(f"Error in wrapped function {name}: {e}")
                    if hasattr(params, "result_callback") and params.result_callback:
                        await params.result_callback(
                            {"error": f"Function execution failed: {str(e)}"}
                        )
                    raise

            self._original_register_function(name, hitl_wrapped_function)
        else:
            # If HITL is not applicable, just use the retry-wrapped function
            self._original_register_function(name, retry_wrapped_function)

    def _add_retry_wrapper(self, name: str, function: Callable) -> Callable:
        """Add a retry wrapper to a function."""
        if not TOOL_RETRY_ENABLE:
            return function

        async def retry_wrapper(params):
            """Wrapper that adds retry logic for tool calls."""
            last_result = None
            for attempt in range(TOOL_MAX_RETRIES):
                future = asyncio.Future()
                original_callback = params.result_callback

                async def new_callback(result):
                    future.set_result(result)

                params.result_callback = new_callback

                try:
                    await function(params)
                    result = await future
                finally:
                    # Restore the original callback
                    params.result_callback = original_callback

                if isinstance(result, dict) and "Tool Error" in result:
                    last_result = result
                    logger.warning(
                        f"Tool call {name} failed with error: {result}. Retrying... (Attempt {attempt + 1}/{TOOL_MAX_RETRIES})"
                    )
                else:
                    await original_callback(result)
                    return

            logger.error(f"Tool call {name} failed after {TOOL_MAX_RETRIES} attempts.")
            if last_result:
                await original_callback(last_result)

        return retry_wrapper

    def create_summarizing_context(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> ContextSummarizer:
        """Create a summarizing context with the given parameters"""
        context = ContextSummarizer(
            messages=messages,
            tools=tools,
            llm_service=self._llm_service,
            max_turns_before_summary=config.MAX_TURNS_BEFORE_SUMMARY,
            keep_recent_turns=config.KEEP_RECENT_TURNS,
            enable_summarization=config.ENABLE_SUMMARIZATION,
        )
        return context

    def __getattr__(self, name):
        return getattr(self._llm_service, name)

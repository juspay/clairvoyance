import uuid
from typing import List, Dict, Any, Optional, Callable
from app.agents.voice.automatic.features.summarizer.context_summarizer import (
    ContextSummarizer,
)
from app.core import config
from app.core.logger import logger
from app.agents.voice.automatic.features.hitl.utils import is_dangerous_operation
from app.agents.voice.automatic.features.hitl.hitl import get_hitl_manager
from app.core.config import HITL_ENABLE


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
        """Wrap function registration to intercept dangerous operations"""
        logger.debug(f"LLM Wrapper: Registering function: {name}")
        self._registered_functions[name] = function
        if HITL_ENABLE:
            is_dangerous = is_dangerous_operation(name)

            if is_dangerous:
                logger.debug(f"LLM Wrapper: Wrapping dangerous function: {name}")

                async def wrapped_function(params):
                    """Wrapper that adds confirmation for dangerous operations"""
                    try:
                        arguments = getattr(params, "arguments", {})
                        tool_call_id = getattr(
                            params, "tool_call_id", str(uuid.uuid4())
                        )
                        result_callback = getattr(params, "result_callback", None)

                        if not result_callback:
                            logger.error(
                                f"No result_callback found for function {name}"
                            )
                            return

                        # Use HITLManager for confirmation
                        hitl_manager = get_hitl_manager()

                        try:
                            # Request confirmation through HITLManager
                            confirmation_result = (
                                await hitl_manager.request_confirmation(
                                    function_name=name,
                                    arguments=arguments,
                                    tool_call_id=tool_call_id,
                                )
                            )

                            # Extract final arguments (may be modified by user)
                            final_args = confirmation_result.get(
                                "modified_arguments", arguments
                            )
                            logger.info(f"User approved function {name}, executing...")

                            # Update params with modified arguments if any
                            if hasattr(params, "arguments"):
                                params.arguments = final_args

                            # Execute the original function
                            result = await function(params)

                            # Add success message
                            success_msg = hitl_manager.generate_success_message(
                                name, final_args
                            )
                            if hasattr(params, "result_callback") and result_callback:
                                if isinstance(result, str):
                                    enhanced_result = f"{result}\n\n{success_msg}"
                                else:
                                    enhanced_result = f"{str(result)}\n\n{success_msg}"
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
                        if (
                            hasattr(params, "result_callback")
                            and params.result_callback
                        ):
                            await params.result_callback(
                                {"error": f"Function execution failed: {str(e)}"}
                            )
                        raise

                self._original_register_function(name, wrapped_function)
            else:
                self._original_register_function(name, function)
        else:

            self._original_register_function(name, function)

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

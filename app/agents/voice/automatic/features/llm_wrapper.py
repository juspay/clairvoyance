import re
import uuid
from typing import Any, Callable, Dict, List, Optional

import httpx
from openai import BadRequestError

from app.agents.voice.automatic.features.hitl.hitl import get_hitl_manager
from app.agents.voice.automatic.features.hitl.utils import is_dangerous_operation
from app.agents.voice.automatic.features.summarizer.context_summarizer import (
    ContextSummarizer,
)
from app.core import config
from app.core.config import HITL_ENABLE
from app.core.logger import logger

# Fallback message when Azure content filter blocks the request
LLM_GUARD_RAIL_POLICY_ERROR_MSG = "I apologize, but I'm unable to process that request due to content policy restrictions. Could you please rephrase your question or ask something else?"


def sanitize_user_message(message: str) -> tuple[str, bool]:
    """
    Sanitize user messages to avoid triggering Azure's content filter.
    Only modifies delete operations with 'rules' to avoid jailbreak detection.
    Returns (sanitized_message, was_modified)
    """
    original = message
    
    # Pattern replacements to avoid jailbreak detection
    # Only replace "delete + rules" patterns, not other operations
    # Allow optional punctuation (comma, period) between words
    patterns = [
        (r'\b(delete|remove)\s*[,.]?\s*(this|that|these|those|the|all)\s*[,.]?\s*rules?\.?\b', r'\1 \2.'),
        (r'\b(delete|remove)\s*[,.]?\s*rules?\.?\b', r'\1 them.'),
    ]
    
    modified = message
    for pattern, replacement in patterns:
        modified = re.sub(pattern, replacement, modified, flags=re.IGNORECASE)
    
    was_modified = (modified != original)
    
    if was_modified:
        logger.info("Sanitized user message to avoid content filter trigger")
        logger.debug("Message sanitization applied for delete/remove rules pattern")
    
    return modified, was_modified


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

        # Wrap the get_chat_completions method to handle content filter errors
        self._original_get_chat_completions = getattr(
            llm_service, "get_chat_completions", None
        )
        if self._original_get_chat_completions:
            logger.debug(
                "LLM Wrapper: Found get_chat_completions method, wrapping it for content filter error handling"
            )
            llm_service.get_chat_completions = self._wrapped_get_chat_completions
        else:
            logger.warning(
                "LLM Wrapper: LLM service does not have get_chat_completions method"
            )

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

    async def _wrapped_get_chat_completions(self, params):
        """Wrap get_chat_completions to handle Azure content filter errors with retry mechanism"""
        try:
            # First attempt: try with original message (no preemptive sanitization)
            logger.debug("LLM Wrapper: Attempting API call with original message")
            return await self._original_get_chat_completions(params)
            
        except (BadRequestError, httpx.HTTPStatusError) as e:
            # Check if this is an Azure content filter error
            error_str = str(e)
            
            # Check for content filter violation
            if "content_filter" in error_str and "ResponsibleAIPolicyViolation" in error_str:
                logger.warning(
                    "Azure content filter triggered (jailbreak detection). Attempting retry with sanitized message."
                )
                logger.debug(f"Full error: {error_str}")
                
                # Extract messages from params to sanitize
                messages = None
                if isinstance(params, dict):
                    messages = params.get('messages')
                elif hasattr(params, 'messages'):
                    messages = params.messages
                
                # Try to sanitize the last user message
                sanitized = False
                if messages and isinstance(messages, list):
                    logger.debug(f"LLM Wrapper: Found {len(messages)} messages, searching for last user message")
                    # Iterate backwards to find the last user message
                    for message in reversed(messages):
                        if isinstance(message, dict) and message.get('role') == 'user':
                            original_content = message.get('content', '')
                            logger.debug(f"LLM Wrapper: Found last user message: '{original_content[:100]}...'")
                            
                            # Apply sanitization
                            sanitized_content, was_modified = sanitize_user_message(original_content)
                            
                            if was_modified:
                                logger.info(f"LLM Wrapper: Sanitized message - Original: '{original_content}' -> Sanitized: '{sanitized_content}'")
                                message['content'] = sanitized_content
                                sanitized = True
                            else:
                                logger.debug("LLM Wrapper: Sanitization patterns did not match this message")
                            break
                else:
                    logger.warning("LLM Wrapper: Could not extract messages from params for sanitization")
                
                # Retry with sanitized message if sanitization was successful
                if sanitized:
                    try:
                        logger.info("LLM Wrapper: Retrying API call with sanitized message")
                        result = await self._original_get_chat_completions(params)
                        logger.info("LLM Wrapper: Retry with sanitized message succeeded!")
                        return result
                    except (BadRequestError, httpx.HTTPStatusError) as retry_error:
                        logger.warning("LLM Wrapper: Retry with sanitized message also failed, using fallback")
                        logger.debug(f"Retry error: {str(retry_error)}")
                else:
                    logger.info("LLM Wrapper: No sanitization applied, using fallback message")

                # Return a generator that yields the fallback message
                # This mimics the streaming response structure
                async def fallback_generator():
                    from types import SimpleNamespace
                    # Create a mock chunk that matches the expected structure
                    # First chunk with content
                    chunk = SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content=LLM_GUARD_RAIL_POLICY_ERROR_MSG,
                                    tool_calls=None,
                                    role=None,
                                    function_call=None
                                ),
                                finish_reason=None,
                                index=0
                            )
                        ],
                        usage=None  # No usage for content chunk
                    )
                    yield chunk
                    
                    # Final chunk with finish_reason
                    final_chunk = SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content=None,
                                    tool_calls=None,
                                    role=None,
                                    function_call=None
                                ),
                                finish_reason="stop",
                                index=0
                            )
                        ],
                        usage=SimpleNamespace(
                            prompt_tokens=0,
                            completion_tokens=0,
                            total_tokens=0,
                            prompt_tokens_details=None,
                            completion_tokens_details=None
                        )
                    )
                    yield final_chunk

                return fallback_generator()

            # If it's not a content filter error, re-raise
            raise

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

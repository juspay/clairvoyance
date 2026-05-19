"""
Unified Transition Handler

This handler replaces all individual handlers. It:
1. Immediately transitions to the next node (synchronous)
2. Triggers hooks asynchronously (fire and forget)
"""

import asyncio
from typing import Any, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.observability.tracing_setup import auto_trace
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.hooks import HookRegistry
from app.ai.voice.agents.breeze_buddy.template.input_collection import (
    get_node_user_speech_timeout,
)
from app.ai.voice.agents.breeze_buddy.template.interruption import (
    apply_node_interruption_config,
    reset_interruption_to_default,
)
from app.ai.voice.agents.breeze_buddy.template.types import HookConfig
from app.ai.voice.agents.breeze_buddy.template.vad import (
    apply_node_vad_config,
    reset_vad_to_default,
)
from app.core.logger import logger


@auto_trace("transition_handler")
async def transition_handler(
    context: TemplateContext,
    args: Dict[str, Any],
    transition_to: Optional[str] = None,
    hooks: Optional[List[Dict[str, Any]]] = None,
    function_name: Optional[str] = None,
):
    """
    Unified handler for all workflow transitions.

    This handler:
    1. Immediately transitions to the next node (if specified)
    2. Executes hooks asynchronously without blocking
    3. Handles VAD parameter reset and node-specific VAD configuration

    Args:
        context: Handler context with bot state access
        args: Function arguments from LLM
        transition_to: Target node to transition to
        hooks: List of hook configuration dictionaries (serialized HookConfig objects)
        function_name: Name of the function that was called

    Returns:
        Tuple of (result_dict, next_node_config) for immediate transition
    """
    logger.info(
        f"Transition handler called - function: '{function_name}', "
        f"transition_to: '{transition_to}', hooks: {hooks}, args: {args}"
    )

    # Execute hooks synchronously (awaited) or asynchronously (fire and forget)
    if hooks:
        awaited = hooks[0].get("awaited", False)
        if awaited:
            logger.info(
                f"Executing {len(hooks)} hook(s) synchronously (awaited) for function '{function_name}'"
            )
            await _execute_hooks_async(context, args, hooks, function_name or "unknown")
        else:
            logger.info(
                f"Scheduling {len(hooks)} hook(s) to execute asynchronously for function '{function_name}'"
            )
            asyncio.create_task(
                _execute_hooks_async(context, args, hooks, function_name or "unknown")
            )
    else:
        logger.debug(f"No hooks to execute for function '{function_name}'")

    # Handle immediate node transition
    if transition_to:
        logger.info(
            f"Transitioning from current node to '{transition_to}' for function '{function_name}'"
        )

        # Record exit from current node (just mark as exited, no via_function)
        context.record_node_exit()

        # Reset VAD params to default before applying node-specific config
        reset_vad_to_default(context)

        # Get node-specific VAD config and apply it
        apply_node_vad_config(context, transition_to)

        # Determine user_speech_timeout from input collection config BEFORE reset.
        # This is passed to both reset and apply so there's never a window where
        # timeout=0.0 is active while transcripts could arrive and trigger an
        # immediate turn end (race condition fix).
        user_speech_timeout = get_node_user_speech_timeout(context, transition_to)

        # Reset interruption strategies to default (with target timeout already set)
        await reset_interruption_to_default(
            context, user_speech_timeout=user_speech_timeout
        )

        # Get node-specific interruption config and apply it (with same timeout)
        await apply_node_interruption_config(
            context, transition_to, user_speech_timeout=user_speech_timeout
        )

        next_node = context.create_node_from_template(transition_to)

        if next_node is None:
            logger.warning(
                f"Node '{transition_to}' not found; skipping traversal recording for function '{function_name}'"
            )
            return {}, None

        # Record entry into new node (pass the function that brought us here)
        context.record_node_entry(transition_to, function_name, args)

        return {}, next_node
    else:
        logger.info(
            f"No transition specified for function '{function_name}', staying in current node"
        )

        result_message = {
            "result": f"Successfully executed {function_name}",
            "status": "success",
        }
        return result_message, None


async def _execute_hooks_async(
    context: TemplateContext,
    args: Dict[str, Any],
    hook_configs: List[Dict[str, Any]],
    function_name: str,
) -> None:
    """
    Execute hooks asynchronously.

    This function runs in the background and doesn't block the main workflow.

    Args:
        context: Handler context with bot state access
        args: Function arguments from LLM
        hook_configs: List of hook configuration dictionaries (serialized HookConfig objects)
        function_name: Name of the function that triggered these hooks
    """
    logger.info(
        f"Starting async execution of {len(hook_configs)} hook(s) for function '{function_name}'"
    )

    for hook_config_dict in hook_configs:
        # Convert dict back to HookConfig object
        hook_config = HookConfig.model_validate(hook_config_dict)
        logger.debug(
            f"Attempting to execute hook '{hook_config.name}' with expected_fields: {hook_config.expected_fields} "
            f"for function '{function_name}'"
        )
        hook = HookRegistry.get(hook_config.name)

        if hook:
            logger.info(
                f"Executing hook '{hook_config.name}' for function '{function_name}'"
            )
            await hook.safe_execute(context, args, function_name, hook_config)
        else:
            logger.warning(
                f"Hook '{hook_config.name}' not found in registry for function '{function_name}'"
            )

    logger.info(
        f"Completed async execution of {len(hook_configs)} hook(s) for function '{function_name}'"
    )

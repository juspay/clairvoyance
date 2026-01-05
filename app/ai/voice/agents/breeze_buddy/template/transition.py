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
from app.ai.voice.agents.breeze_buddy.template.types import HookConfig
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

    # Schedule hooks to run asynchronously (fire and forget)
    if hooks:
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
        next_node = context.create_node_from_template(transition_to)
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
            await hook.safe_execute(
                context, args, function_name, hook_config.expected_fields
            )
        else:
            logger.warning(
                f"Hook '{hook_config.name}' not found in registry for function '{function_name}'"
            )

    logger.info(
        f"Completed async execution of {len(hook_configs)} hook(s) for function '{function_name}'"
    )

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
from app.ai.voice.agents.breeze_buddy.template.types import FieldSource, HookConfig
from app.ai.voice.agents.breeze_buddy.template.vad import (
    apply_node_vad_config,
    reset_vad_to_default,
)
from app.core.logger import logger


def _eagerly_set_outcome_from_hooks(
    context: TemplateContext,
    hook_configs: List[Dict[str, Any]],
    function_name: str,
) -> None:
    """
    Eagerly set the lead outcome in-memory from hook configurations.

    This prevents a race condition where:
    1. Hooks are scheduled asynchronously (fire-and-forget)
    2. The client disconnects before the hooks finish executing
    3. _handle_unexpected_disconnect sees outcome=None and sets it to "BUSY"
    4. This triggers a retry, resulting in duplicate tags (e.g., both
       buddy-failed and buddy-confirmed on the same order)

    By setting the outcome synchronously before the async hooks run,
    the disconnect handler will see the correct outcome and won't
    override it with the default "BUSY" value.
    """
    if not context.lead:
        return

    for hook_config_dict in hook_configs:
        try:
            hook_config = HookConfig.model_validate(hook_config_dict)
            if (
                hook_config.name == "update_outcome_in_database"
                and hook_config.expected_fields
            ):
                outcome_field = hook_config.expected_fields.get("outcome")
                if (
                    outcome_field
                    and outcome_field.source == FieldSource.STATIC
                    and outcome_field.value
                ):
                    context.lead.outcome = outcome_field.value
                    logger.info(
                        f"Eagerly set lead outcome to '{outcome_field.value}' "
                        f"for function '{function_name}' to prevent race condition "
                        f"with client disconnect"
                    )
                    return
        except Exception as e:
            logger.warning(
                f"Failed to eagerly extract outcome from hook config "
                f"for function '{function_name}': {e}"
            )


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

    # Schedule hooks to run asynchronously (fire and forget)
    if hooks:
        # Eagerly set the outcome in-memory before async hooks run.
        # This prevents a race condition where a client disconnect before
        # the async hook completes would cause outcome=None to be overridden
        # with "BUSY", triggering an incorrect retry and duplicate tags.
        _eagerly_set_outcome_from_hooks(context, hooks, function_name or "unknown")

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

        # Reset VAD params to default before applying node-specific config
        reset_vad_to_default(context)

        # Get node-specific VAD config and apply it
        apply_node_vad_config(context, transition_to)

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
            await hook.safe_execute(context, args, function_name, hook_config)
        else:
            logger.warning(
                f"Hook '{hook_config.name}' not found in registry for function '{function_name}'"
            )

    logger.info(
        f"Completed async execution of {len(hook_configs)} hook(s) for function '{function_name}'"
    )

"""
Function-level pre/post action handlers for global functions.

This module maintains all func_pre_action and func_post_action handlers in one place.
Each handler receives (context, response_data, action_args) and performs
a side-effect (e.g., updating payload in DB).
"""

import re
from typing import Any, Callable, Dict, List

from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.field_resolver import (
    replace_placeholders,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import FlowAction
from app.core.logger import logger
from app.database.accessor.breeze_buddy.lead_call_tracker import update_lead_payload

# ---------------------------------------------------------------------------
# Post-action handler registry
# ---------------------------------------------------------------------------

FUNC_POST_ACTION_HANDLERS: Dict[str, Callable] = {}


async def _handle_update_payload(
    context: TemplateContext,
    response_data: Dict[str, Any],
    action_args: Dict[str, Any],
) -> None:
    """
    Post-action handler that updates lead payload fields from API response data.

    Resolves field_mapping templates against response data and persists the
    resolved values to lead_call_tracker.payload (JSON merge), context.lead.payload
    (in-memory), and context.bot.template_vars (for template variable substitution).

    Args:
        context: TemplateContext with access to lead and bot
        response_data: The 'data' field from the API response
        action_args: Must contain 'field_mapping' dict, e.g.
                     {"customer_name": "{firstName} {lastName}"}
    """
    field_mapping = action_args.get("field_mapping")
    if not field_mapping:
        logger.warning("update_payload post-action called without field_mapping")
        return

    # Flatten response data for placeholder resolution.
    # response_data is the raw API response body (already parsed JSON).
    if not isinstance(response_data, dict):
        logger.warning(
            f"update_payload: response_data is not a dict, got {type(response_data)}"
        )
        return

    # Filter out None values from response data so placeholders for missing
    # fields remain unresolved (e.g. {lastName} stays as-is when lastName is null)
    clean_data = {k: v for k, v in response_data.items() if v is not None}

    # Resolve each field_mapping template against response data
    resolved: Dict[str, Any] = {}
    for field_name, template in field_mapping.items():
        if isinstance(template, str):
            resolved_value = replace_placeholders(template, clean_data)
            # Strip any remaining unreplaced {placeholder} tokens (field was None)
            resolved_value = re.sub(r"\{[^}]+\}", "", resolved_value).strip()
            if not resolved_value:
                logger.info(
                    f"update_payload: skipping field '{field_name}' — "
                    f"all placeholders resolved to None"
                )
                continue
            resolved[field_name] = resolved_value
        else:
            # Non-string values are passed through as-is
            resolved[field_name] = template

    if not resolved:
        logger.warning("update_payload: no fields resolved from field_mapping")
        return

    logger.info(f"update_payload: resolved fields: {list(resolved.keys())}")

    # 1. Update in-memory payload on context.lead
    lead = context.lead
    if lead and lead.payload:
        lead.payload.update(resolved)
    elif lead:
        lead.payload = resolved

    # 2. Update template_vars so {customer_name} substitution works immediately
    context.bot.template_vars.update(resolved)

    # 3. Persist to DB
    if lead and lead.id:
        await update_lead_payload(lead.id, resolved)


# Register the update_payload handler
FUNC_POST_ACTION_HANDLERS["update_payload"] = _handle_update_payload


# ---------------------------------------------------------------------------
# Post-action execution engine
# ---------------------------------------------------------------------------


async def execute_func_post_actions(
    bot_instance: Any,
    result: Any,
    post_actions: List[FlowAction],
    function_name: str,
) -> None:
    """
    Execute func_post_actions for a global function in a fire-and-forget manner.

    Only executes when the function response indicates success.
    Errors are logged but never propagated.

    Args:
        bot_instance: Bot instance for creating TemplateContext
        result: The (response_dict, None) tuple returned by the handler
        post_actions: List of FlowAction configs to execute
        function_name: Name of the global function (for logging)
    """
    try:
        # Unpack the result tuple — handlers return (response_dict, None)
        response = result[0] if isinstance(result, tuple) else result

        # Only execute post-actions on success
        if not isinstance(response, dict) or response.get("status") != "success":
            logger.debug(
                f"Skipping func_post_actions for '{function_name}': "
                f"response status is not 'success'"
            )
            return

        context = TemplateContext(bot_instance)

        for action in post_actions:
            if action.type != "function":
                logger.warning(
                    f"Unsupported func_post_action type '{action.type}' "
                    f"for global function '{function_name}'"
                )
                continue

            handler_name = action.handler
            if not handler_name:
                logger.warning(
                    f"func_post_action for '{function_name}' has no handler specified"
                )
                continue

            handler = FUNC_POST_ACTION_HANDLERS.get(handler_name)
            if not handler:
                logger.warning(
                    f"func_post_action handler '{handler_name}' not found in "
                    f"FUNC_POST_ACTION_HANDLERS for function '{function_name}'. "
                    f"Available: {list(FUNC_POST_ACTION_HANDLERS.keys())}"
                )
                continue

            logger.info(
                f"Executing func_post_action '{handler_name}' "
                f"for global function '{function_name}'"
            )
            await handler(context, response.get("data", {}), action.args or {})

    except Exception as e:
        logger.error(
            f"Error executing func_post_actions for global function '{function_name}': {e}",
            exc_info=True,
        )

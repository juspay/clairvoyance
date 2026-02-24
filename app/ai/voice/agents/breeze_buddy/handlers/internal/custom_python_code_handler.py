"""
Custom Python Handler

Runtime execution handler for custom Python global functions.
Invoked when LLM calls a custom function.

Follows the same pattern as http_function_handler and builtin_function_dispatcher.
"""

from typing import Any, Dict, Optional

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.global_function import (
    execute_custom_function,
)
from app.ai.voice.agents.breeze_buddy.template.types import GlobalCustomFunction
from app.core.logger import logger


async def custom_python_code_handler(
    context: TemplateContext,
    args: Dict[str, Any],
    function_config: Optional[GlobalCustomFunction] = None,
) -> tuple[dict[str, Any], None]:
    """
    Execute a custom Python global function.

    This handler:
    1. Validates that function_config has a compiled handler
    2. Builds the read-only context dict with lead info
    3. Executes the user code with timeout handling
    4. Wraps result in standard response envelope

    Args:
        context: TemplateContext with bot state access
        args: Arguments dict from LLM function call
        function_config: GlobalCustomFunction with compiled_handler attached

    Returns:
        Tuple of (result_dict, None) - None means stay on current node
        result_dict has shape: {"status": "success"/"error", "data"/"error": ...}
    """
    if function_config is None:
        logger.error(
            "[custom_python_code_handler] function_config is required but was None"
        )
        return {
            "status": "error",
            "error": "Function configuration not provided",
        }, None

    if (
        not hasattr(function_config, "compiled_handler")
        or function_config.compiled_handler is None
    ):
        logger.error(
            f"[custom_python_code_handler] {function_config.name}: compiled_handler not found"
        )
        return {
            "status": "error",
            "error": "Custom function not compiled",
        }, None

    handler_fn = function_config.compiled_handler
    timeout = getattr(function_config, "timeout_seconds", 5)

    # Build read-only context dict
    # Note: Keep surface minimal in v1, expand only as concrete needs emerge
    user_context = {
        "lead": context.lead.model_dump() if context.lead else {},
        "call_sid": context.call_sid,
        "lead_id": getattr(context.lead, "id", None) if context.lead else None,
        # Future expansion: transcripts, node history, merchant config
    }

    logger.info(
        f"[custom_python_code_handler] Executing '{function_config.name}' with timeout={timeout}s"
    )

    try:
        result = await execute_custom_function(
            handler=handler_fn,
            args=args,
            context=user_context,
            timeout=timeout,
        )

        # Wrap successful result
        logger.info(
            f"[custom_python_code_handler] '{function_config.name}' completed successfully"
        )
        return {
            "status": "success",
            "data": result,
        }, None

    except TimeoutError as e:
        logger.warning(
            f"[custom_python_code_handler] '{function_config.name}' timed out: {e}"
        )
        return {
            "status": "error",
            "error": str(e),
        }, None

    except Exception as e:
        logger.error(
            f"[custom_python_code_handler] '{function_config.name}' runtime error: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }, None

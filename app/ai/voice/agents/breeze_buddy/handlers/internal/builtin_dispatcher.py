"""
Built-in Function Dispatcher

Central dispatcher for built-in global functions. Routes to the correct
handler based on function_config.handler field.

To add a new built-in global function:
1. Create a handler in handlers/internal/ with signature: (context, args) -> Dict
2. Import it here and add to BUILTIN_HANDLERS registry
3. Use it in templates with: {"type": "builtin", "handler": "<handler_key>", ...}
"""

from typing import Any, Callable, Dict, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.handlers.internal.get_current_time import (
    get_current_time,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.warm_transfer import (
    connect_to_live_agent,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import GlobalBuiltinFunction
from app.core.logger import logger

# Registry of built-in handler name -> handler function.
# Each handler has signature: (context: TemplateContext, args: Dict) -> Dict
# To add a new built-in function, import it and add an entry here.
BUILTIN_HANDLERS: Dict[str, Callable] = {
    "connect_to_live_agent": connect_to_live_agent,
    "get_current_time": get_current_time,
}


async def builtin_function_dispatcher(
    context: TemplateContext,
    args: Dict[str, Any],
    function_config: Optional[GlobalBuiltinFunction] = None,
) -> Tuple[Dict[str, Any], None]:
    """
    Dispatch to the correct built-in handler based on function_config.handler.

    This dispatcher is registered in handler_map and wrapped with with_context.
    It receives function_config from the BuiltinGlobalFunctionAdapter and routes
    to the actual handler.

    Args:
        context: TemplateContext with bot state access
        args: LLM function arguments
        function_config: GlobalBuiltinFunction with handler name

    Returns:
        Tuple of (result_dict, None) - None means stay on current node
    """
    if function_config is None:
        logger.error("[builtin_dispatcher] function_config is required but was None")
        return {
            "status": "error",
            "error": "Function configuration not provided",
        }, None

    handler_name = function_config.handler
    handler = BUILTIN_HANDLERS.get(handler_name)

    if handler is None:
        logger.error(
            f"[builtin_dispatcher] Unknown handler '{handler_name}'. "
            f"Available: {list(BUILTIN_HANDLERS.keys())}"
        )
        return {
            "status": "error",
            "error": f"Unknown built-in handler: {handler_name}",
        }, None

    logger.info(
        f"[builtin_dispatcher] Dispatching '{function_config.name}' -> '{handler_name}'"
    )

    try:
        result = await handler(context, args)
        # Ensure consistent return format (result, None) for global functions
        if isinstance(result, tuple):
            return result
        return result, None
    except Exception as e:
        logger.error(
            f"[builtin_dispatcher] Error in handler '{handler_name}': {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "error": str(e),
        }, None

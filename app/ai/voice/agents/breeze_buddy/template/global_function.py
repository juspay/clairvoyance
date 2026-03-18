"""
Global Function Adapters for extensible function types.

This module provides an adapter pattern for building global functions,
allowing new function types to be added without modifying the core builder logic.

The adapters follow the same context injection pattern as normal functions:
- Handlers are wrapped with `with_context(bot_instance)` in agent.py
- The wrapped handler receives (context, args, function_config=...) from with_context
- This eliminates the need for _bot_instance storage in flow_manager.state
"""

import asyncio
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from pipecat_flows import FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.template.func_action_handlers import (
    execute_func_post_actions,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    GlobalBuiltinFunction,
    GlobalFunctionType,
    GlobalHttpFunction,
)
from app.core.config.static import GLOBAL_FUNCTION_DESCRIPTION_SUFFIX
from app.core.logger import logger


@runtime_checkable
class GlobalFunctionAdapter(Protocol):
    """Protocol that all global function adapters must implement."""

    @property
    def function_type(self) -> GlobalFunctionType:
        """Return the function type this adapter handles."""
        ...

    @property
    def handler_name(self) -> str:
        """
        Return the handler name this adapter requires from handler_map.

        This allows each adapter to specify which handler it needs,
        enabling multiple global function types with different handlers.

        Example:
            - HttpGlobalFunctionAdapter returns "http_function_handler"
            - DatabaseGlobalFunctionAdapter returns "database_function_handler"
        """
        ...

    def can_handle(self, config: Dict[str, Any]) -> bool:
        """
        Check if this adapter can handle the given config.

        Args:
            config: Raw function configuration dict from flow JSON

        Returns:
            True if this adapter can handle this config
        """
        ...

    def build_schema(
        self,
        config: Dict[str, Any],
        wrapped_handler: Callable,
        bot_instance: Any = None,
    ) -> FlowsFunctionSchema:
        """
        Build a FlowsFunctionSchema from the config.

        Args:
            config: Raw function configuration dict
            wrapped_handler: Handler already wrapped with with_context(bot_instance)
            bot_instance: Bot instance for creating TemplateContext in func_post_actions

        Returns:
            FlowsFunctionSchema ready for FlowManager
        """
        ...


class HttpGlobalFunctionAdapter:
    """
    Adapter for HTTP-based global functions.

    Handles configs with 'http_request' key, e.g.:
    {
        "name": "check_order_status",
        "description": "Check the status of a customer order",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
        "http_request": {
            "method": "GET",
            "url": "https://api.example.com/orders/{order_id}"
        }
    }

    Uses the same with_context pattern as normal functions:
    - The http_function_handler is wrapped with with_context in agent.py
    - This adapter creates an outer wrapper that passes function_config via kwargs
    - with_context extracts function_config and passes it to the raw handler
    """

    @property
    def function_type(self) -> GlobalFunctionType:
        return GlobalFunctionType.HTTP

    @property
    def handler_name(self) -> str:
        """Return the handler name required from handler_map."""
        return "http_function_handler"

    def can_handle(self, config: Dict[str, Any]) -> bool:
        """
        HTTP adapter handles configs with type='http' or 'http_request' key.

        Supports two ways to identify HTTP functions:
        1. Explicit type: {"type": "http", ...}
        2. Implicit (legacy): {"http_request": {...}, ...}
        """
        # Check explicit type first
        if config.get("type") == GlobalFunctionType.HTTP.value:
            return True
        # Fall back to checking for http_request key (implicit/legacy)
        return "http_request" in config

    def build_schema(
        self,
        config: Dict[str, Any],
        wrapped_handler: Callable,
        bot_instance: Any = None,
    ) -> FlowsFunctionSchema:
        """
        Build FlowsFunctionSchema for HTTP global function.

        Args:
            config: Raw function configuration dict
            wrapped_handler: http_function_handler wrapped with with_context(bot_instance)
            bot_instance: Bot instance for creating TemplateContext in func_post_actions

        Returns:
            FlowsFunctionSchema with handler that passes function_config via kwargs
        """
        # Validate and parse config
        func = GlobalHttpFunction.model_validate(config)

        # Enhance description with continuation prompt suffix
        enhanced_description = func.description
        if GLOBAL_FUNCTION_DESCRIPTION_SUFFIX:
            enhanced_description = (
                f"{func.description} {GLOBAL_FUNCTION_DESCRIPTION_SUFFIX}"
            )

        logger.debug(
            f"Building HTTP global function: {func.name}, "
            f"http_request={func.http_request.method.value} {func.http_request.url}"
        )

        # Create outer wrapper that passes function_config via kwargs
        # This follows the same pattern as normal functions passing transition_to, hooks, etc.
        def create_wrapper(
            captured_func: GlobalHttpFunction, captured_bot_instance: Any
        ):
            async def wrapper_handler(llm_args, flow_manager):
                """
                Outer wrapper for global HTTP function.

                Calls the wrapped handler (with_context wrapper) passing function_config
                via kwargs. The with_context wrapper will extract function_config and
                pass it to http_function_handler.
                """
                result = await wrapped_handler(
                    llm_args,
                    function_config=captured_func,
                )
                if captured_func.func_post_actions and captured_bot_instance:
                    asyncio.create_task(
                        execute_func_post_actions(
                            captured_bot_instance,
                            result,
                            captured_func.func_post_actions,
                            captured_func.name,
                        )
                    )
                return result

            return wrapper_handler

        return FlowsFunctionSchema(
            name=func.name,
            description=enhanced_description,
            handler=create_wrapper(func, bot_instance),
            properties=func.properties,
            required=func.required,
        )


class BuiltinGlobalFunctionAdapter:
    """
    Adapter for built-in global functions (e.g., warm transfer, get current time).

    Built-in functions are internal handlers that can be exposed as global functions
    via template configuration. The adapter routes through a single dispatcher handler
    that resolves the actual handler from the builtin registry.

    Template config example:
        {
            "type": "builtin",
            "name": "transfer_to_agent",
            "handler": "connect_to_live_agent",
            "description": "Transfer the call to a human agent when requested"
        }

    To add a new built-in function:
    1. Create a handler in handlers/internal/ with signature: (context, args) -> Dict
    2. Add it to BUILTIN_HANDLERS in builtin_dispatcher.py
    3. Use it in templates with: {"type": "builtin", "handler": "<key>", ...}
    """

    @property
    def function_type(self) -> GlobalFunctionType:
        return GlobalFunctionType.BUILTIN

    @property
    def handler_name(self) -> str:
        """Return the dispatcher handler name from handler_map."""
        return "builtin_function_dispatcher"

    def can_handle(self, config: Dict[str, Any]) -> bool:
        """Check if config has type='builtin'."""
        return config.get("type") == GlobalFunctionType.BUILTIN.value

    def build_schema(
        self,
        config: Dict[str, Any],
        wrapped_handler: Callable,
        bot_instance: Any = None,
    ) -> FlowsFunctionSchema:
        """
        Build FlowsFunctionSchema for a built-in global function.

        Args:
            config: Raw function configuration dict with 'handler' field
            wrapped_handler: builtin_function_dispatcher wrapped with with_context
            bot_instance: Bot instance for creating TemplateContext in func_post_actions

        Returns:
            FlowsFunctionSchema with handler that passes function_config to dispatcher
        """
        func = GlobalBuiltinFunction.model_validate(config)

        enhanced_description = func.description
        if GLOBAL_FUNCTION_DESCRIPTION_SUFFIX:
            enhanced_description = (
                f"{func.description} {GLOBAL_FUNCTION_DESCRIPTION_SUFFIX}"
            )

        logger.debug(
            f"Building builtin global function: {func.name}, handler={func.handler}"
        )

        def create_wrapper(
            captured_func: GlobalBuiltinFunction, captured_bot_instance: Any
        ):
            async def wrapper_handler(llm_args, flow_manager):
                """
                Outer wrapper for built-in global function.

                Passes function_config to the dispatcher via kwargs.
                The with_context wrapper extracts function_config and passes
                it to builtin_function_dispatcher.
                """
                result = await wrapped_handler(
                    llm_args,
                    function_config=captured_func,
                )
                if captured_func.func_post_actions and captured_bot_instance:
                    asyncio.create_task(
                        execute_func_post_actions(
                            captured_bot_instance,
                            result,
                            captured_func.func_post_actions,
                            captured_func.name,
                        )
                    )
                return result

            return wrapper_handler

        return FlowsFunctionSchema(
            name=func.name,
            description=enhanced_description,
            handler=create_wrapper(func, bot_instance),
            properties=func.properties,
            required=func.required,
        )


class GlobalFunctionRegistry:
    """
    Registry for global function adapters.

    This class maintains a mapping of adapter names to adapter instances.
    Follows the same pattern as HookRegistry for consistency.

    Usage:
        # Register at module level (bottom of file)
        GlobalFunctionRegistry.register("http", HttpGlobalFunctionAdapter())

        # Build global functions (in builder.py)
        schemas = GlobalFunctionRegistry.build(flow, wrapped_handler)
    """

    _adapters: Dict[str, GlobalFunctionAdapter] = {}

    @classmethod
    def register(cls, name: str, adapter: GlobalFunctionAdapter) -> None:
        """
        Register a global function adapter.

        Args:
            name: Name to register the adapter under (e.g., "http", "database")
            adapter: Adapter instance
        """
        cls._adapters[name] = adapter
        logger.info(f"Registered global function adapter: {name}")

    @classmethod
    def get(cls, name: str) -> Optional[GlobalFunctionAdapter]:
        """
        Get an adapter by name.

        Args:
            name: Name of the adapter

        Returns:
            Adapter instance or None if not found
        """
        return cls._adapters.get(name)

    @classmethod
    def get_all(cls) -> Dict[str, GlobalFunctionAdapter]:
        """
        Get all registered adapters.

        Returns:
            Dictionary of all adapters
        """
        return cls._adapters.copy()

    @classmethod
    def build(
        cls,
        flow: Dict[str, Any],
        handler_map: Dict[str, Callable],
        bot_instance: Any = None,
    ) -> List[FlowsFunctionSchema]:
        """
        Build all global functions from flow config using registered adapters.

        Each adapter declares which handler it needs via the handler_name property.
        This allows different global function types to use different handlers.

        Args:
            flow: Flow configuration dict containing optional 'global_functions' array
            handler_map: Dictionary mapping handler names to wrapped handlers.
                        Handlers should already be wrapped with with_context(bot_instance).
                        Example: {"http_function_handler": wrapped_http_handler, ...}
            bot_instance: Bot instance for creating TemplateContext in func_post_actions

        Returns:
            List of FlowsFunctionSchema objects to pass to FlowManager
        """
        global_functions_config = flow.get("global_functions", [])
        if not global_functions_config:
            logger.debug("No global_functions defined in flow config")
            return []

        logger.info(f"Building {len(global_functions_config)} global functions")

        result: List[FlowsFunctionSchema] = []

        for func_config in global_functions_config:
            schema = cls._build_one(func_config, handler_map, bot_instance)
            if schema:
                result.append(schema)

        logger.info(f"Built {len(result)} global functions successfully")
        return result

    @classmethod
    def _build_one(
        cls,
        func_config: Dict[str, Any],
        handler_map: Dict[str, Callable],
        bot_instance: Any = None,
    ) -> Optional[FlowsFunctionSchema]:
        """
        Build a single global function using matching adapter.

        Args:
            func_config: Raw function configuration from template JSON
            handler_map: Dictionary of handler_name -> wrapped_handler
            bot_instance: Bot instance for creating TemplateContext in func_post_actions

        Returns:
            FlowsFunctionSchema or None if no adapter found or build failed
        """
        for adapter in cls._adapters.values():
            if adapter.can_handle(func_config):
                # Get the handler this adapter needs
                handler = handler_map.get(adapter.handler_name)
                if not handler:
                    logger.error(
                        f"Handler '{adapter.handler_name}' not found in handler_map "
                        f"for adapter '{adapter.function_type}'. "
                        f"Available handlers: {list(handler_map.keys())}"
                    )
                    return None

                try:
                    return adapter.build_schema(
                        func_config, handler, bot_instance=bot_instance
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to build global function from config: {str(e)}",
                        exc_info=True,
                    )
                    return None

        logger.warning(f"No adapter found for function: {func_config.get('name')}")
        return None


# Register adapters at module load time (same pattern as HookRegistry)
GlobalFunctionRegistry.register("http", HttpGlobalFunctionAdapter())
GlobalFunctionRegistry.register("builtin", BuiltinGlobalFunctionAdapter())

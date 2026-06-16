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
import inspect
import random
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from pipecat_flows import FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.template.approval import gate_global_function
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.func_action_handlers import (
    execute_func_post_actions,
)
from app.ai.voice.agents.breeze_buddy.template.session_state import (
    _inject_voice_state,
    _reduce_voice_state,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    BaseGlobalFunction,
    GlobalBuiltinFunction,
    GlobalCustomFunction,
    GlobalFunctionType,
    GlobalHttpFunction,
    PhrasingOrder,
)
from app.ai.voice.agents.breeze_buddy.utils.parser import (
    compile_custom_function,
)
from app.core.config.static import GLOBAL_FUNCTION_DESCRIPTION_SUFFIX
from app.core.logger import logger

# Execution budget assumed for a handler whose template sets no explicit
# timeout_secs, used when sizing the watchdog for approval-gated functions.
# Must be >= the LLM services' function_call_timeout_secs default (10s).
_APPROVAL_EXEC_FALLBACK_SECS = 60.0
_APPROVAL_BUDGET_MARGIN_SECS = 15.0


def _flows_async_kwargs(func: BaseGlobalFunction) -> Dict[str, Any]:
    """Per-function pipecat-flows 1.0 kwargs, opt-in via template config.

    `cancel_on_interruption=False` makes the call async — the LLM continues
    the turn without waiting and the result is injected as a developer
    message later. When unset on the model, flows' own default applies
    (False / async since pipecat-flows 1.0). Sync vs. async behaviour is
    therefore controlled by the per-subclass model defaults — `GlobalHttp
    Function` defaults to async (False) for slow APIs and `GlobalBuiltin
    Function` defaults to sync (True) for control-flow critical handlers.

    Approval-gated functions get an ADDITIVE watchdog budget (approval wait
    + execution + margin), never max(): pipecat's watchdog does NOT cancel
    the handler — it fires result_callback(None), the aggregator marks the
    call COMPLETED, and any later real result is dropped while the side
    effect still executes. An undersized budget therefore means a silent
    bot, a dropped result, and a possible LLM retry / duplicate execution.
    """
    extra: Dict[str, Any] = {}
    if func.cancel_on_interruption is not None:
        extra["cancel_on_interruption"] = func.cancel_on_interruption
    if func.timeout_secs is not None:
        extra["timeout_secs"] = func.timeout_secs
    if func.approval is not None:
        exec_budget = (
            func.timeout_secs
            if func.timeout_secs is not None
            else _APPROVAL_EXEC_FALLBACK_SECS
        )
        extra["timeout_secs"] = (
            func.approval.timeout_secs + exec_budget + _APPROVAL_BUDGET_MARGIN_SECS
        )
    return extra


def _make_global_wrapper(
    func: BaseGlobalFunction,
    wrapped_handler: Callable,
    bot_instance: Any,
) -> Callable:
    """Build the outer handler shared by all global function adapters.

    Execution side effects (filler phrase, background music, fire-and-forget
    func_post_actions) live INSIDE the ``execute`` closure handed to the
    approval gate, so a denied/timed-out gated call plays no filler and
    fires no post-actions. On the approve path the behavior is identical to
    the pre-refactor wrappers: filler/music start, handler runs, music stops
    (even on error), post-actions fire on the result.
    """

    async def wrapper_handler(llm_args, flow_manager):
        # SessionStatePolicy (voice): inject state-driven args before the
        # handler runs and lift identifiers off the result after it — the
        # same reducers / tool_arg_injection chat applies. Chat sets
        # handles_state_externally and does this in its own _cycle_loop, so
        # skip here to avoid double-application.
        manages_state = not getattr(bot_instance, "handles_state_externally", False)
        effective_args = (
            _inject_voice_state(bot_instance, func.name, llm_args)
            if manages_state
            else llm_args
        )

        async def execute() -> Any:
            await _run_filler_and_music(bot_instance, func)
            try:
                result = await wrapped_handler(
                    effective_args,
                    function_config=func,
                )
            finally:
                # Always stop music even if handler errors
                await _stop_music(bot_instance, func)

            if manages_state:
                _reduce_voice_state(bot_instance, func.name, result)

            if func.func_post_actions and bot_instance:
                asyncio.create_task(
                    execute_func_post_actions(
                        bot_instance,
                        result,
                        func.func_post_actions,
                        func.name,
                    )
                )
            return result

        return await gate_global_function(bot_instance, func, effective_args, execute)

    return wrapper_handler


async def _run_filler_and_music(
    bot_instance: Any,
    func: BaseGlobalFunction,
) -> None:
    """Queue filler TTS and/or enable background music before a global function runs.

    Both configs are independent and can run together:
    - filler_phrase_config: queues a TTS phrase first so the user hears it immediately.
    - background_music_config: MixerEnableFrame queued after TTSSpeakFrame so music
      only starts after filler playback ends (enforced by Pipecat's frame ordering).
    """
    if not bot_instance or not func.filler_audio:
        return

    context = TemplateContext(bot_instance)
    filler_audio = func.filler_audio

    # Phrase is queued first so the user hears it while waiting.
    # MixerEnableFrame is queued after TTSSpeakFrame — Pipecat processes frames
    # in order, so music only starts after filler playback ends.
    phrase_cfg = filler_audio.filler_phrase_config
    if phrase_cfg and phrase_cfg.phrases:
        if phrase_cfg.phrasing_order == PhrasingOrder.RANDOM:
            phrase = random.choice(phrase_cfg.phrases)
        else:  # SEQUENTIAL — stateless: always picks phrases[0] (first phrase only)
            phrase = phrase_cfg.phrases[0]
        try:
            await context.queue_tts_filler(phrase)
            logger.info(f"[{func.name}] Filler phrase queued: {phrase!r}")
        except Exception as e:
            logger.warning(f"[{func.name}] Failed to queue filler phrase: {e}")

    music_cfg = filler_audio.background_music_config
    if music_cfg and music_cfg.sound_file:
        try:
            soundtrack_key = (
                music_cfg.sound_file.value
                if hasattr(music_cfg.sound_file, "value")
                else music_cfg.sound_file
            )
            await context.manage_audio_mixer(
                enable=True,
                settings={"sound": soundtrack_key, "volume": music_cfg.volume},
            )
            logger.info(f"[{func.name}] Background music enabled")
        except Exception as e:
            logger.warning(f"[{func.name}] Failed to enable background music: {e}")


async def _stop_music(bot_instance: Any, func: BaseGlobalFunction) -> None:
    """Disable background music after a global function handler completes."""
    if not bot_instance:
        return

    filler_audio = func.filler_audio
    if (
        filler_audio
        and filler_audio.background_music_config
        and filler_audio.background_music_config.sound_file
    ):
        context = TemplateContext(bot_instance)
        try:
            await context.manage_audio_mixer(enable=False)
        except Exception as e:
            logger.warning(f"[{func.name}] Failed to disable background music: {e}")


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
    ) -> Optional[FlowsFunctionSchema]:
        """
        Build a FlowsFunctionSchema from the config.

        Args:
            config: Raw function configuration dict
            wrapped_handler: Handler already wrapped with with_context(bot_instance)
            bot_instance: Bot instance for creating TemplateContext in func_post_actions

        Returns:
            FlowsFunctionSchema ready for FlowManager, or None if build failed
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

        return FlowsFunctionSchema(
            name=func.name,
            description=enhanced_description,
            handler=_make_global_wrapper(func, wrapped_handler, bot_instance),
            properties=func.properties,
            required=func.required,
            **_flows_async_kwargs(func),
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

        return FlowsFunctionSchema(
            name=func.name,
            description=enhanced_description,
            handler=_make_global_wrapper(func, wrapped_handler, bot_instance),
            properties=func.properties,
            required=func.required,
            **_flows_async_kwargs(func),
        )


class CustomPythonGlobalFunctionAdapter:
    """
    Adapter for custom Python global functions.

    Custom functions allow developers to write Python code directly in templates.
    The python_code is compiled at build time and executed when the LLM calls the function.

    Template config example:
        {
            "type": "custom",
            "name": "calculate_discount",
            "description": "Calculate discount based on order count",
            "properties": {"order_count": {"type": "integer"}},
            "required": ["order_count"],
            "python_code": "def handler(args, context):\\n    n = args['order_count']\\n    if n > 50:\\n        return {'tier': 'gold'}\\n    return {'tier': 'bronze'}",
            "timeout_seconds": 5
        }

    The handler entry point must be a top-level function named 'handler' accepting
    two arguments: (args, context). It can be sync or async.
    """

    @property
    def function_type(self) -> GlobalFunctionType:
        return GlobalFunctionType.CUSTOM

    @property
    def handler_name(self) -> str:
        """Return the handler name required from handler_map."""
        return "custom_python_code_handler"

    def can_handle(self, config: Dict[str, Any]) -> bool:
        """Check if config has type='custom'."""
        return config.get("type") == GlobalFunctionType.CUSTOM.value

    def build_schema(
        self,
        config: Dict[str, Any],
        wrapped_handler: Callable,
        bot_instance: Any = None,
    ) -> Optional[FlowsFunctionSchema]:
        """
        Build FlowsFunctionSchema for a custom Python global function.

        Compiles the python_code at build time and attaches the compiled handler
        to the function config for use at runtime.

        Args:
            config: Raw function configuration dict with 'python_code' field
            wrapped_handler: custom_python_code_handler wrapped with with_context
            bot_instance: Bot instance for creating TemplateContext in func_post_actions

        Returns:
            FlowsFunctionSchema or None if compilation failed
        """
        func = GlobalCustomFunction.model_validate(config)

        # Compile python_code at build time
        compiled = compile_custom_function(func.name, func.python_code)
        if compiled is None:
            # Compilation failed - log warning and skip this function
            # Other functions in the template continue normally
            logger.warning(
                f"Skipping custom function '{func.name}' due to compilation failure"
            )
            return None

        # Attach compiled handler to function config
        func.compiled_handler = compiled

        enhanced_description = func.description
        if GLOBAL_FUNCTION_DESCRIPTION_SUFFIX:
            enhanced_description = (
                f"{func.description} {GLOBAL_FUNCTION_DESCRIPTION_SUFFIX}"
            )

        logger.debug(
            f"Building custom Python global function: {func.name}, "
            f"timeout={func.timeout_seconds}s"
        )

        return FlowsFunctionSchema(
            name=func.name,
            description=enhanced_description,
            handler=_make_global_wrapper(func, wrapped_handler, bot_instance),
            properties=func.properties,
            required=func.required,
            **_flows_async_kwargs(func),
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
GlobalFunctionRegistry.register("custom", CustomPythonGlobalFunctionAdapter())


async def execute_custom_function(
    handler: Callable,
    args: Dict[str, Any],
    context: Dict[str, Any],
    timeout: int = 5,
) -> Any:
    """
    Execute the custom handler with timeout handling.

    Handles both sync and async handlers.

    Args:
        handler: The compiled handler function
        args: Arguments dict from LLM
        context: Context dict with lead info
        timeout: Maximum execution time in seconds

    Returns:
        Handler result

    Raises:
        TimeoutError: If execution exceeds timeout
        Exception: Any exception raised by handler
    """
    try:
        if inspect.iscoroutinefunction(handler):
            # Async handler - await directly with timeout
            return await asyncio.wait_for(handler(args, context), timeout=timeout)
        else:
            # Sync handler - run in thread pool to avoid blocking
            return await asyncio.wait_for(
                asyncio.to_thread(handler, args, context), timeout=timeout
            )
    except asyncio.TimeoutError:
        raise TimeoutError(f"Function timed out after {timeout}s")

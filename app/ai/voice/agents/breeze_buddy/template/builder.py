"""
Flow Configuration Builder

This module builds Pipecat flow configurations from database models.
"""

from typing import AbstractSet, Any, Callable, Dict, List, Optional, Set, cast

from pipecat.flows import (
    FlowManager,
    FlowsDirectFunction,
    FlowsFunctionSchema,
    NodeConfig,
)
from pipecat.flows.types import ActionConfig, FlowResult

from app.ai.voice.agents.breeze_buddy.handlers.internal import (
    builtin_function_dispatcher,
    connect_to_live_agent,
    custom_python_code_handler,
    end_conversation,
    mute_stt,
    play_audio_sound,
    unmute_stt,
)
from app.ai.voice.agents.breeze_buddy.handlers.transport.http_handler import (
    http_function_handler,
)
from app.ai.voice.agents.breeze_buddy.template.global_function import (
    GlobalFunctionRegistry,
)
from app.ai.voice.agents.breeze_buddy.template.kb_tool import (
    append_kb_tool,
    synthesize_kb_tool_function,
)
from app.ai.voice.agents.breeze_buddy.template.transition import (
    transition_handler,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ActionType,
    FlowAction,
    FlowFunction,
    FlowMode,
    FlowNodeModel,
    GlobalFunctionType,
    TemplateModel,
)
from app.ai.voice.agents.breeze_buddy.template.ui_prompt import (
    render_primitives_section,
)
from app.core.logger import logger

# Synthetic node name used in direct mode. There is only ever one node so the
# concrete name does not matter, but it must be stable across the build/init
# code paths and unlikely to collide with a user-defined node.
DIRECT_MODE_NODE_NAME = "__direct__"

# Literal placeholder in template prompts (system_prompt for direct mode,
# task_messages / role_messages for flow mode) that the builder replaces
# with the rendered "## Available primitives" section at build time.
UI_PRIMITIVES_PLACEHOLDER = "{{ui_primitives_section}}"


def _splice_ui_primitives(text: str, ui_allowlist: Optional[Set[str]]) -> str:
    """Substitute ``{{ui_primitives_section}}`` with the rendered section.

    When ``ui_allowlist`` is ``None`` the placeholder is replaced with an
    empty string — keeps templates that haven't opted into the catalog
    flow path working unchanged, and lets test fixtures opt out without
    importing the catalog. When the placeholder isn't present the input
    is returned untouched (fast path; most non-chat templates).
    """
    if UI_PRIMITIVES_PLACEHOLDER not in text:
        return text
    if ui_allowlist is None:
        return text.replace(UI_PRIMITIVES_PLACEHOLDER, "")
    return text.replace(
        UI_PRIMITIVES_PLACEHOLDER, render_primitives_section(ui_allowlist)
    )


# Function dicts whose ``type`` is one of these are routed through the global
# function adapter registry; everything else is treated as a FlowFunction.
_GLOBAL_FUNCTION_TYPES = {t.value for t in GlobalFunctionType}


def filter_disabled_identifiers(
    items: List[Dict[str, Any]],
    disabled: AbstractSet[str],
    log_label: str = "item",
) -> List[Dict[str, Any]]:
    """Drop dicts whose ``name``, ``function_name`` *or* ``handler`` is disabled.

    All three identifier keys are checked: a single ``or`` chain would let
    an authored entry like ``{"name": "transfer_to_finance", "handler":
    "connect_to_live_agent"}`` slip past, because ``name`` resolves first
    and isn't in the disabled set even though the handler binding is.
    Action entries (which carry only ``handler``) and function entries
    (``name`` / ``function_name``) are both covered by the union check.

    Returns the input list as-is when ``disabled`` is empty (no copy
    made), so call sites can route through this helper unconditionally
    without paying for an allocation in the common case.

    A ``WARNING`` is logged for each stripped item, suffixed with
    ``log_label`` so authors can locate the affected JSON array.
    """
    if not disabled:
        return items

    out: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            present: set[str] = {
                str(item[k])
                for k in ("name", "function_name", "handler")
                if isinstance(item.get(k), str)
            }
            blocked = present & disabled
            if blocked:
                logger.warning(
                    f"Stripping disabled {log_label} (matched {sorted(blocked)})"
                )
                continue
        out.append(item)
    return out


class FlowConfigBuilder:
    """Builds Pipecat flow configurations from database models"""

    def __init__(
        self,
        disabled_names: AbstractSet[str] = frozenset(),
        *,
        quiet: bool = False,
    ):
        """
        Initialize builder with a static handler map.

        The handler_map contains raw handlers that will be wrapped with
        with_context(bot_instance) in agent.py before use. The map is
        the same regardless of caller — channel-specific behaviour is
        realised by filtering template-level references via
        ``filter_disabled_identifiers`` rather than swapping handler
        bodies.

        Note: Global function adapters are registered at module level in
        global_function_handler.py (same pattern as HookRegistry).

        Args:
            disabled_names: Identifiers to drop from per-node functions,
                global functions, and per-node pre/post actions before
                pydantic validation. Voice agents pass nothing (default
                empty set, no-op fast-path inside the filter); chat
                agents pass ``CHAT_DISABLED_NAMES`` (see
                ``app.ai.voice.agents.breeze_buddy.chat.disabled``).
            quiet: When True, demote the per-build INFO log lines
                (``Building flow config``, ``Built direct-mode flow
                config``, ``Direct mode: built N function(s)``,
                ``Built flow config with N nodes``) to DEBUG. Chat
                rebuilds the flow per turn — keep INFO for voice's
                once-per-call setup but skip the noise on follow-up
                chat turns.
        """
        self._disabled_names = disabled_names
        # Pre-bind so call sites stay terse: ``self._log(...)`` instead
        # of ``(logger.debug if quiet else logger.info)(...)`` per call.
        self._log = logger.debug if quiet else logger.info

        self.handler_map = {
            "mute_stt": mute_stt,
            "unmute_stt": unmute_stt,
            "play_audio_sound": play_audio_sound,
            "end_conversation": end_conversation,
            "transition_handler": transition_handler,
            "connect_to_live_agent": connect_to_live_agent,
            "http_function_handler": http_function_handler,
            "builtin_function_dispatcher": builtin_function_dispatcher,
            "custom_python_code_handler": custom_python_code_handler,
        }

    def build_flow_config(
        self,
        template: TemplateModel,
        *,
        ui_allowlist: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """
        Convert database template to Pipecat flow format.

        Args:
            template: Template configuration from database
            ui_allowlist: Optional set of UI-catalog primitive names the
                LLM may emit on this session. When set, the builder
                splices the rendered ``## Available primitives`` section
                into every ``{{ui_primitives_section}}`` placeholder it
                finds in the template's system_prompt / task_messages /
                role_messages. When ``None`` (the voice / no-chat default),
                the placeholder is replaced with an empty string so
                templates remain channel-agnostic.

        Returns:
            Dictionary in Pipecat flow format with transitions

        Raises:
            ValueError: If initial node is not found or flow structure is invalid
        """
        self._log(
            f"Building flow config from template: {template.name if hasattr(template, 'name') else 'unknown'}"
        )

        # Extract initial_node and nodes from flow JSON
        flow = template.flow
        if not flow:
            logger.error("Flow structure is empty in template")
            raise ValueError("Flow structure is empty")

        # Direct mode: synthesize a single virtual node from a global system
        # prompt. Reuses the rest of the flow path unchanged so all features
        # (filler audio, hooks, evaluators, OTEL, greeting injection) work.
        if flow.get("mode") == FlowMode.DIRECT.value:
            return self._build_direct_flow_config(template, ui_allowlist=ui_allowlist)

        initial_node_name = flow.get("initial_node")
        if not initial_node_name:
            logger.error("initial_node not found in flow structure")
            raise ValueError("initial_node not found in flow structure")

        logger.debug(f"Initial node name: {initial_node_name}")

        nodes_data = flow.get("nodes", [])
        if not nodes_data:
            logger.error("No nodes found in flow structure")
            raise ValueError("No nodes found in flow structure")

        logger.debug(f"Found {len(nodes_data)} nodes in flow structure")

        # Convert nodes data to FlowNodeModel objects
        flow_nodes = []
        for node_data in nodes_data:
            # Transform function_name to name for compatibility
            transformed_node_data = node_data.copy()
            if "functions" in transformed_node_data:
                # Templates are now L2-cached (``template/cache.py``) and
                # ``node_data.copy()`` is shallow — the inner ``functions``
                # list and its dicts are still cache-owned. Materialise a
                # fresh list of shallow-copied dicts so the rename below
                # (``pop("function_name")``) doesn't rewrite the cached
                # template in place and corrupt subsequent turns.
                transformed_node_data["functions"] = [
                    dict(func) if isinstance(func, dict) else func
                    for func in filter_disabled_identifiers(
                        transformed_node_data["functions"],
                        self._disabled_names,
                        "function",
                    )
                ]
                for func in transformed_node_data["functions"]:
                    if isinstance(func, dict) and (
                        "function_name" in func and "name" not in func
                    ):
                        func["name"] = func.pop("function_name")

            # In chat mode, also strip FUNCTION-type pre/post actions whose
            # handler is voice-only — otherwise they'd attempt to invoke STT
            # mute / audio playback against a transport that has neither.
            for action_field in ("pre_actions", "post_actions"):
                if action_field in transformed_node_data:
                    transformed_node_data[action_field] = filter_disabled_identifiers(
                        transformed_node_data[action_field],
                        self._disabled_names,
                        "action",
                    )

            flow_nodes.append(FlowNodeModel.model_validate(transformed_node_data))
            logger.debug(
                f"Validated node: {transformed_node_data.get('node_name', 'unknown')}"
            )

        # Find initial node
        initial_node = next(
            (n for n in flow_nodes if n.node_name == initial_node_name),
            None,
        )
        if not initial_node:
            logger.error(f"Initial node not found: {initial_node_name}")
            raise ValueError(f"Initial node not found: {initial_node_name}")

        logger.debug(f"Found initial node: {initial_node_name}")

        # Build nodes dictionary
        nodes = {}
        for node in flow_nodes:
            logger.debug(f"Building node: {node.node_name}")
            nodes[node.node_name] = self._build_node(node, ui_allowlist=ui_allowlist)

        # Extract end_conversation_callbacks if present
        end_conversation_callbacks = flow.get("end_conversation_callbacks", [])
        logger.debug(f"End conversation callbacks: {end_conversation_callbacks}")

        self._log(
            f"Built flow config with {len(nodes)} nodes, initial: {initial_node_name}, "
            f"callbacks: {end_conversation_callbacks}"
        )

        return {
            "initial_node": initial_node_name,
            "nodes": nodes,
            "end_conversation_callbacks": end_conversation_callbacks,
            "expected_callback_response_schema": template.expected_callback_response_schema,
        }

    def _build_direct_flow_config(
        self,
        template: TemplateModel,
        *,
        ui_allowlist: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """
        Build a flow config for direct mode (one global system prompt, no nodes).

        Internally synthesizes a single node named ``DIRECT_MODE_NODE_NAME`` so
        the rest of the flow path (FlowManager.initialize, prepare_initial_node,
        etc.) works unchanged. All user-defined functions are exposed as global
        functions — there are no per-node functions because there is only one
        virtual node.

        VAD / interruption / input-collection settings are intentionally not
        accepted here: they already exist at ``template.configurations.*`` and
        apply globally when no per-node override is set, so direct mode just
        relies on those.

        Args:
            template: Template configuration from database

        Returns:
            Dictionary in Pipecat flow format with a single synthesized node.

        Raises:
            ValueError: If ``system_prompt`` is missing.
        """
        flow = template.flow
        system_prompt = flow.get("system_prompt")
        if not system_prompt or not isinstance(system_prompt, str):
            raise ValueError(
                "Direct mode requires a non-empty 'system_prompt' string in flow"
            )

        # Splice the rendered "## Available primitives" section into the
        # template prompt before it reaches the LLM. Templates without the
        # placeholder are untouched; templates with a placeholder but no
        # allowlist (voice / pre-chat) get an empty-string replacement so
        # the same template can serve both channels.
        system_prompt = _splice_ui_primitives(system_prompt, ui_allowlist)

        # system_prompt goes into role_messages so pipecat-flows prepends it
        # ahead of task_messages. When a greeting is played, prepare_initial_node
        # prepends an assistant greeting message to task_messages, giving the LLM
        # an [system_prompt, assistant_greeting, user_reply] order rather than
        # the awkward [assistant_greeting, system_prompt, user_reply] order that
        # putting system_prompt in task_messages would produce. Language rules
        # injected by inject_language_rules append to role_messages naturally.
        role_messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

        pre_actions = []
        raw_pre_actions = filter_disabled_identifiers(
            flow.get("pre_actions") or [],
            self._disabled_names,
            "action",
        )
        if raw_pre_actions:
            self._log(f"Direct mode: parsing {len(raw_pre_actions)} pre_actions")
            pre_actions = [
                self._build_action(FlowAction.model_validate(act))
                for act in raw_pre_actions
            ]

        node_config = NodeConfig(  # type: ignore[no-matching-overload]
            name=DIRECT_MODE_NODE_NAME,
            task_messages=[],
            role_messages=role_messages,
            functions=cast(List[FlowsFunctionSchema], []),
            pre_actions=cast(List[ActionConfig], pre_actions),
            post_actions=cast(List[ActionConfig], []),
        )

        end_conversation_callbacks = flow.get("end_conversation_callbacks", [])

        self._log(
            f"Built direct-mode flow config: 1 synthesized node "
            f"({DIRECT_MODE_NODE_NAME}), system_prompt={len(system_prompt)} chars, "
            f"end_conversation_callbacks={end_conversation_callbacks}"
        )

        return {
            "initial_node": DIRECT_MODE_NODE_NAME,
            "nodes": {DIRECT_MODE_NODE_NAME: node_config},
            "end_conversation_callbacks": end_conversation_callbacks,
            "expected_callback_response_schema": template.expected_callback_response_schema,
            "mode": FlowMode.DIRECT.value,
        }

    def build_global_functions(
        self, flow: Dict[str, Any], bot_instance: Any = None
    ) -> List[FlowsFunctionSchema]:
        """
        Build global functions from template flow config using registered adapters.

        Global functions are available to the LLM from any node in the conversation.
        They can make HTTP requests, database queries, or execute custom logic and
        return the response to the LLM for decision-making.

        Each adapter declares which handler it needs via the handler_name property.
        The handler_map (with all handlers wrapped with with_context in agent.py)
        is passed to the registry, which resolves the correct handler for each adapter.

        Args:
            flow: Flow configuration dict containing optional 'global_functions' array
            bot_instance: Bot instance for post-action context creation

        Returns:
            List of FlowsFunctionSchema objects to pass to FlowManager(global_functions=[...])
        """
        # Tool-mode knowledge base: synthesize the query_knowledge_base
        # builtin from configurations.knowledge_base so one config section
        # drives all retrieval modes (see template/kb_tool.py). Voice and
        # chat both flow through this method, so the tool appears uniformly
        # on both channels.
        kb_tool_function = synthesize_kb_tool_function(bot_instance, log=self._log)

        # Direct mode has a single flat `functions` array. Each entry is
        # routed by `type`: http/builtin/custom go through the global-function
        # adapter registry; everything else is a FlowFunction (hooks-only).
        # The KB tool is appended BEFORE the disabled filter so per-channel
        # disabling applies to it like any other function.
        if flow.get("mode") == FlowMode.DIRECT.value:
            declared_functions = append_kb_tool(
                flow.get("functions") or [], kb_tool_function
            )
            direct_functions = filter_disabled_identifiers(
                declared_functions, self._disabled_names, "function"
            )
            return self._build_direct_mode_functions(
                direct_functions,
                bot_instance=bot_instance,
            )

        # Flow mode: keep the legacy split between per-node `functions` (built
        # by `_build_function_schema` during node construction) and top-level
        # `global_functions` (built here via the adapter registry). The
        # filter is a no-op in voice mode (returns the same list), so we can
        # always run it; we shallow-copy the flow dict to avoid mutating the
        # caller's structure when assigning the filtered list back.
        global_functions = filter_disabled_identifiers(
            append_kb_tool(flow.get("global_functions") or [], kb_tool_function),
            self._disabled_names,
            "function",
        )
        flow_for_globals = {
            **flow,
            "global_functions": global_functions,
        }

        return GlobalFunctionRegistry.build(
            flow_for_globals,
            handler_map=self.handler_map,
            bot_instance=bot_instance,
        )

    def _build_direct_mode_functions(
        self,
        functions: List[Dict[str, Any]],
        bot_instance: Any = None,
    ) -> List[FlowsFunctionSchema]:
        """Convert a direct-mode functions array to FlowsFunctionSchemas.

        Routes each entry by type:
          - ``type`` ∈ {"http", "builtin", "custom"} → global function adapter
          - otherwise → FlowFunction (hooks-only, no transition)
        """
        global_entries: List[Dict[str, Any]] = []
        flow_function_models: List[FlowFunction] = []

        for func_raw in functions:
            if not isinstance(func_raw, dict):
                logger.warning(
                    f"Direct mode: ignoring non-dict function entry: {type(func_raw)}"
                )
                continue

            func_data = func_raw.copy()
            if "function_name" in func_data and "name" not in func_data:
                func_data["name"] = func_data.pop("function_name")

            if func_data.get("type") in _GLOBAL_FUNCTION_TYPES:
                global_entries.append(func_data)
            else:
                # Direct mode never transitions — silently drop any
                # `transition_to` so old templates copied across modes don't
                # accidentally try to navigate to a non-existent node.
                func_data.pop("transition_to", None)
                flow_function_models.append(FlowFunction.model_validate(func_data))

        result: List[FlowsFunctionSchema] = []
        if global_entries:
            result.extend(
                GlobalFunctionRegistry.build(
                    {"global_functions": global_entries},
                    handler_map=self.handler_map,
                    bot_instance=bot_instance,
                )
            )
        for func_model in flow_function_models:
            result.append(self._build_function_schema(func_model))

        self._log(
            f"Direct mode: built {len(result)} function(s) "
            f"({len(global_entries)} adapter-routed, {len(flow_function_models)} hook-only)"
        )
        return result

    def _build_node(
        self,
        node: FlowNodeModel,
        *,
        ui_allowlist: Optional[Set[str]] = None,
    ) -> NodeConfig:
        """
        Build NodeConfig from FlowNodeModel.

        Args:
            node: Flow node model from database
            ui_allowlist: Optional UI primitive allowlist used to splice
                ``{{ui_primitives_section}}`` placeholders in task /
                role messages.

        Returns:
            NodeConfig object
        """
        logger.debug(f"Building NodeConfig for node: {node.node_name}")

        task_messages = [
            {
                "role": msg.role,
                "content": _splice_ui_primitives(msg.content, ui_allowlist),
            }
            for msg in node.task_messages
        ]
        logger.debug(f"Node {node.node_name} has {len(task_messages)} task messages")

        role_messages = [
            {
                "role": msg.role,
                "content": _splice_ui_primitives(msg.content, ui_allowlist),
            }
            for msg in node.role_messages
        ]
        logger.debug(f"Node {node.node_name} has {len(role_messages)} role messages")

        functions = []
        if node.functions:
            logger.debug(f"Node {node.node_name} has {len(node.functions)} functions")
            functions = [self._build_function_schema(func) for func in node.functions]
        else:
            logger.debug(f"Node {node.node_name} has no functions")

        pre_actions = []
        if node.pre_actions:
            logger.debug(
                f"Node {node.node_name} has {len(node.pre_actions)} pre_actions"
            )
            pre_actions = [self._build_action(action) for action in node.pre_actions]
        else:
            logger.debug(f"Node {node.node_name} has no pre_actions")

        post_actions = []
        if node.post_actions:
            logger.debug(
                f"Node {node.node_name} has {len(node.post_actions)} post_actions"
            )
            post_actions = [self._build_action(action) for action in node.post_actions]
        else:
            logger.debug(f"Node {node.node_name} has no post_actions")

        logger.info(
            f"Built NodeConfig for {node.node_name}: "
            f"{len(functions)} functions, {len(pre_actions)} pre_actions, {len(post_actions)} post_actions, "
            f"{len(task_messages)} task_messages, {len(role_messages)} role_messages"
        )

        # Build NodeConfig dictionary - cast lists to expected types
        node_config = NodeConfig(  # type: ignore[no-matching-overload]
            name=node.node_name,
            task_messages=task_messages,
            role_messages=role_messages,
            functions=cast(List[FlowsDirectFunction | FlowsFunctionSchema], functions),
            pre_actions=cast(List[ActionConfig], pre_actions),
            post_actions=cast(List[ActionConfig], post_actions),
        )

        # Attach node-specific VAD config for transition handler to use
        # NodeConfig is a TypedDict - use cast to allow custom key
        if node.vad_config:
            cast(Dict[str, Any], node_config)["vad_config"] = node.vad_config
            logger.info(f"Attached VAD config to node {node.node_name}")

        # Attach node-specific interruption config for transition handler to use
        if node.interruption:
            cast(Dict[str, Any], node_config)["interruption"] = node.interruption
            logger.info(
                f"Attached interruption config to node {node.node_name}: "
                f"mode={node.interruption.mode.value}, min_words={node.interruption.min_words}"
            )

        # Attach node-specific input collection config for transition handler to use
        if node.input_collection:
            cast(Dict[str, Any], node_config)[
                "input_collection"
            ] = node.input_collection
            logger.info(
                f"Attached input collection config to node {node.node_name}: "
                f"enabled={node.input_collection.enabled}, "
                f"user_speech_timeout={node.input_collection.user_speech_timeout}s"
            )

        return node_config

    def _build_function_schema(self, func: FlowFunction) -> FlowsFunctionSchema:
        """
        Build FlowsFunctionSchema from database model for dynamic flows.
        Uses transition_handler with hooks for async execution.

        Args:
            func: Function schema from database

        Returns:
            Pipecat FlowsFunctionSchema object with unified handler
        """
        logger.debug(
            f"Building function schema for: {func.name}, "
            f"transition_to={func.transition_to}, hooks={func.hooks}"
        )

        # Get the wrapped unified handler from handler_map
        wrapped_unified_handler = self.handler_map.get("transition_handler")
        if not wrapped_unified_handler:
            logger.error("transition_handler not found in handler_map")
            raise ValueError("transition_handler not found in handler_map")

        # Convert HookConfig objects to dicts to make them JSON serializable
        hooks = [hook.model_dump() for hook in func.hooks] if func.hooks else []
        logger.debug(f"Using hooks for {func.name}: {hooks}")

        # Create a wrapper handler matching FlowsFunctionSchema expected signature.
        # In flows 1.0, ConsolidatedFunctionResult is (FlowResult | None, NodeConfig | None);
        # the legacy str-node-name variant of next_node was removed.
        async def wrapper_handler(
            llm_args: Dict[str, Any], _flow_manager: FlowManager
        ) -> FlowResult | tuple[FlowResult | None, NodeConfig | None]:
            # Call the wrapped unified transition handler
            # The with_context wrapper expects llm_args as first positional arg
            result = await cast(Callable[..., Any], wrapped_unified_handler)(
                llm_args,
                transition_to=func.transition_to,
                hooks=hooks,
                function_name=func.name,
            )
            return result

        logger.debug(f"Successfully built function schema for: {func.name}")

        return FlowsFunctionSchema(
            name=func.name,
            description=func.description,
            handler=wrapper_handler,
            properties=func.properties,
            required=func.required,
        )

    def _build_action(self, action: FlowAction) -> Dict[str, Any]:
        """
        Build action configuration.

        Args:
            action: Action configuration (FlowAction)

        Returns:
            Action configuration dictionary
        """
        logger.debug(
            f"Building action: type={action.type}, handler={action.handler if hasattr(action, 'handler') else 'N/A'}, "
            f"args={action.args if hasattr(action, 'args') else 'N/A'}"
        )

        action_type = action.type
        if action_type == ActionType.TTS_SAY:
            logger.debug(
                f"Building TTS_SAY action with text: {action.text[:50] if action.text else 'empty'}..."
            )
            return {"type": "tts_say", "text": action.text or ""}
        elif action_type == ActionType.FUNCTION:
            if not action.handler:
                logger.error("FUNCTION action requires a handler name")
                raise ValueError("FUNCTION action requires a handler name")
            handler = self.handler_map.get(action.handler)
            if not handler:
                logger.error(f"Handler not found for name: {action.handler}")
                raise ValueError(f"Handler not found for name: {action.handler}")
            logger.debug(
                f"Successfully built FUNCTION action for handler: {action.handler} with args: {action.args}"
            )
            result: Dict[str, Any] = {"type": "function", "handler": handler}
            if action.args:
                result["args"] = action.args
            return result
        else:
            logger.warning(f"Unknown action type: {action_type}, returning as string")
            return {"type": str(action_type)}

"""
Flow Configuration Builder

This module builds Pipecat flow configurations from database models.
"""

from typing import Any, Callable, Dict, List, cast

from pipecat_flows import (
    FlowManager,
    FlowsDirectFunction,
    FlowsFunctionSchema,
    NodeConfig,
)
from pipecat_flows.types import ActionConfig, FlowResult

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
from app.ai.voice.agents.breeze_buddy.template.transition import (
    transition_handler,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ActionType,
    FlowAction,
    FlowFunction,
    FlowNodeModel,
    TemplateModel,
)
from app.core.logger import logger


class FlowConfigBuilder:
    """Builds Pipecat flow configurations from database models"""

    def __init__(self):
        """
        Initialize builder with a static handler map.

        The handler_map contains raw handlers that will be wrapped with
        with_context(bot_instance) in agent.py before use.

        Note: Global function adapters are registered at module level in
        global_function_handler.py (same pattern as HookRegistry).
        """
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

    def build_flow_config(self, template: TemplateModel) -> Dict[str, Any]:
        """
        Convert database template to Pipecat flow format.

        Args:
            template: Template configuration from database

        Returns:
            Dictionary in Pipecat flow format with transitions

        Raises:
            ValueError: If initial node is not found or flow structure is invalid
        """
        logger.info(
            f"Building flow config from template: {template.name if hasattr(template, 'name') else 'unknown'}"
        )

        # Extract initial_node and nodes from flow JSON
        flow = template.flow
        if not flow:
            logger.error("Flow structure is empty in template")
            raise ValueError("Flow structure is empty")

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
                for func in transformed_node_data["functions"]:
                    if "function_name" in func and "name" not in func:
                        func["name"] = func.pop("function_name")

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
            nodes[node.node_name] = self._build_node(node)

        # Extract end_conversation_callbacks if present
        end_conversation_callbacks = flow.get("end_conversation_callbacks", [])
        logger.debug(f"End conversation callbacks: {end_conversation_callbacks}")

        logger.info(
            f"Built flow config with {len(nodes)} nodes, initial: {initial_node_name}, "
            f"callbacks: {end_conversation_callbacks}"
        )

        return {
            "initial_node": initial_node_name,
            "nodes": nodes,
            "end_conversation_callbacks": end_conversation_callbacks,
            "expected_callback_response_schema": template.expected_callback_response_schema,
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
        # Pass entire handler_map - each adapter will pick the handler it needs
        # All handlers are already wrapped with with_context(bot_instance) in agent.py
        return GlobalFunctionRegistry.build(
            flow, handler_map=self.handler_map, bot_instance=bot_instance
        )

    def _build_node(self, node: FlowNodeModel) -> NodeConfig:
        """
        Build NodeConfig from FlowNodeModel.

        Args:
            node: Flow node model from database

        Returns:
            NodeConfig object
        """
        logger.debug(f"Building NodeConfig for node: {node.node_name}")

        task_messages = [
            {"role": msg.role, "content": msg.content} for msg in node.task_messages
        ]
        logger.debug(f"Node {node.node_name} has {len(task_messages)} task messages")

        role_messages = [
            {"role": msg.role, "content": msg.content} for msg in node.role_messages
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

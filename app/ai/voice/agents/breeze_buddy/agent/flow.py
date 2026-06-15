"""Flow management and node configuration for voice agents."""

from typing import Any, Dict, List, Optional, cast

from pipecat.services.azure.llm import AzureLLMService
from pipecat_flows import FlowManager, NodeConfig
from pipecat_flows.types import FlowsDirectFunction, FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.agent.utils import validate_template_compat
from app.ai.voice.agents.breeze_buddy.template import (
    FlowConfigBuilder,
)
from app.ai.voice.agents.breeze_buddy.template.loader import FlowConfigLoader
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    TemplateModel,
)
from app.ai.voice.agents.breeze_buddy.utils.language_utils.prompt_injections import (
    inject_language_rules,
)
from app.ai.voice.agents.breeze_buddy.utils.playground import (
    apply_playground_overrides,
)
from app.core.logger import logger
from app.schemas.breeze_buddy.core import ExecutionMode, LeadCallTracker


async def load_template_config(
    lead: LeadCallTracker,
) -> tuple[TemplateModel, Optional[ConfigurationModel], Dict[str, str]]:
    """Load template configuration from database.

    Args:
        lead: The lead instance

    Returns:
        Tuple of (template, configurations, template_vars)
    """
    flow_loader = FlowConfigLoader()

    template, template_vars = await flow_loader.load_template(
        reseller_id=lead.reseller_id,
        template=lead.template,
        merchant_id=lead.merchant_id if lead else None,
        call_payload=lead.payload,
        template_id=lead.template_id,
        lead_id=lead.id if lead else None,
    )

    # Apply overrides and re-render if playground mode
    template = apply_playground_overrides(lead, template, template_vars)

    # DAILY_STREAM is client-driven STT/TTS-only — no LLM is ever built, so
    # LLM-config compatibility checks (realtime + flow-mode rejection,
    # realtime + keyword_filter rejection) cannot fire at runtime. Skip the
    # check so a stream session can reuse a template that's also configured
    # for agent-mode without spurious load failures. Agent-mode loads still
    # validate normally and surface misconfiguration loudly.
    if getattr(lead, "execution_mode", None) != ExecutionMode.DAILY_STREAM:
        validate_template_compat(template)

    return template, template.configurations, template_vars


def setup_flow_manager(
    task: Any,
    llm: AzureLLMService,
    context_aggregator: Any,
    transport: Any,
    flow_builder: FlowConfigBuilder,
    template: TemplateModel,
    bot_instance: Any = None,
    mcp_global_functions: Optional[List[FlowsFunctionSchema]] = None,
) -> FlowManager:
    """Set up the flow manager with global functions.

    Args:
        task: The pipeline task
        llm: LLM service
        context_aggregator: Context aggregator
        transport: Transport instance
        flow_builder: Flow config builder
        template: Template model
        bot_instance: Bot instance for post-action context creation
        mcp_global_functions: Optional MCP tools converted to FlowsFunctionSchema

    Returns:
        Configured FlowManager
    """
    global_functions = flow_builder.build_global_functions(
        flow=template.flow, bot_instance=bot_instance
    )

    # Merge MCP global functions if present (avoiding name collisions)
    if mcp_global_functions:
        existing_names = {fn.name for fn in global_functions}
        mcp_names = [fn.name for fn in mcp_global_functions]
        collisions = [name for name in mcp_names if name in existing_names]
        if collisions:
            logger.warning(
                f"[BUDDY_MCP] Skipping duplicate MCP tool names: {sorted(set(collisions))}"
            )
        unique_mcp_functions = [
            fn for fn in mcp_global_functions if fn.name not in existing_names
        ]
        global_functions.extend(unique_mcp_functions)
        logger.info(
            f"[BUDDY_MCP] Added {len(unique_mcp_functions)} MCP tools as global functions"
        )

    if global_functions:
        logger.info(
            f"Registering {len(global_functions)} global functions with FlowManager"
        )

    return FlowManager(
        task=task,
        llm=llm,
        context_aggregator=context_aggregator,
        transport=transport,
        global_functions=cast(
            Optional[List[FlowsDirectFunction | FlowsFunctionSchema]],
            global_functions or None,
        ),
    )


def build_flow_config(
    flow_builder: FlowConfigBuilder,
    template: TemplateModel,
) -> tuple[Dict[str, Any], List, Any]:
    """Build flow configuration from template.

    Args:
        flow_builder: Flow config builder
        template: Template model

    Returns:
        Tuple of (flow_config, end_conversation_callbacks, expected_callback_response_schema)
    """
    flow_config = flow_builder.build_flow_config(template)
    end_conversation_callbacks = flow_config.get("end_conversation_callbacks", [])
    expected_callback_response_schema = flow_config.get(
        "expected_callback_response_schema", None
    )

    # Propagate data-source system messages set by the loader so that
    # prepare_initial_node can prepend them to the initial node's context.
    ds_messages = template.flow.get("_data_source_messages")
    if ds_messages:
        flow_config["_data_source_messages"] = ds_messages

    logger.info(
        f"Built flow config with {len(flow_config['nodes'])} nodes, "
        f"initial: {flow_config['initial_node']}, "
        f"end_conversation_callbacks: {end_conversation_callbacks}"
    )

    return flow_config, end_conversation_callbacks, expected_callback_response_schema


def prepare_initial_node(
    flow_config: Dict[str, Any],
    lead_payload: dict,
    configurations: Optional[ConfigurationModel],
    has_greeting_source: bool,
    greeting_text: Optional[str] = None,
) -> NodeConfig:
    """Prepare the initial node configuration with language injection.

    Args:
        flow_config: The flow configuration dict
        lead_payload: Lead payload data
        configurations: Template configurations
        has_greeting_source: Whether a greeting source exists
        greeting_text: The resolved greeting text that was played to the user

    Returns:
        Configured NodeConfig for the initial node
    """
    initial_node_name = flow_config["initial_node"]
    node_config = flow_config["nodes"][initial_node_name]

    role_messages = inject_language_rules(
        node_config.get("role_messages", []),
        lead_payload.get("language_name", "English"),
        getattr(configurations, "payload_based_language_selection", False),
    )

    # Add greeting context to task messages if greeting was played
    task_messages = list(node_config["task_messages"])
    if greeting_text and has_greeting_source:
        # Inject the greeting as an assistant message so LLM knows what was said
        greeting_context_message = {
            "role": "assistant",
            "content": greeting_text,
        }
        # Prepend the greeting message to task messages
        task_messages = [greeting_context_message] + task_messages
        logger.info(f"Injected greeting into LLM context: {greeting_text[:50]}...")

    # Prepend data-source "message" injections (inject_as="message")
    # These are system messages containing fetched sheet content that the
    # LLM needs as read-only context before starting the conversation.
    ds_messages = flow_config.get("_data_source_messages")
    if ds_messages:
        task_messages = ds_messages + task_messages
        logger.info(
            "Prepended %d data-source system message(s) to initial node",
            len(ds_messages),
        )

    return NodeConfig(
        name=node_config["name"],
        task_messages=task_messages,
        role_messages=role_messages,
        functions=node_config.get("functions", []),
        pre_actions=node_config.get("pre_actions", []),
        post_actions=node_config.get("post_actions", []),
        respond_immediately=not has_greeting_source,
    )


def prepare_resume_node(
    flow_config: Dict[str, Any],
    lead_payload: dict,
    configurations: Optional[ConfigurationModel],
    *,
    start_node_name: Optional[str],
    prior_history: List[Dict[str, Any]],
) -> NodeConfig:
    """Prepare a NodeConfig that resumes an existing conversation.

    Used by the unified widget mode (CHAT_MODE.md §14) when voice is
    attached to an in-progress chat session: we want the bot to start
    at ``start_node_name`` with the chat's full message history
    already in the LLM context.

    Why this works (verified against pipecat-flows source at
    .venv/lib/.../pipecat_flows/manager.py:712-825):

      FlowManager always queues an ``LLMMessagesUpdateFrame`` (RESET)
      on the FIRST ``_set_node`` call, regardless of context_strategy
      — the condition is ``self._current_node is None``. A naive
      "pre-seed LLMContext, then call initialize(node)" would have
      the aggregator REPLACE the seed with [role + task] of the node,
      wiping our history.

      The clean workaround stays inside the official API: we put
      ``prior_history`` at the tail of ``task_messages``. The RESET
      frame then sets context to
      [role_messages, task_messages, ...prior_history] in one shot —
      the order we want. Each prior_history entry already has its own
      ``{role: "user"|"assistant", content: str}``; the LLM doesn't
      require role-homogeneous task_messages.

    ``start_node_name`` defaults to ``flow_config["initial_node"]``
    when None — covers the case where chat hadn't transitioned past
    the initial node yet.

    No greeting injection: when resuming we never want the bot to
    re-greet (the greeting is already in prior_history if the chat
    had one). ``respond_immediately=True`` so the bot responds to
    whatever the user says first instead of speaking an empty turn.
    """
    node_name = start_node_name or flow_config["initial_node"]
    if node_name not in flow_config["nodes"]:
        # Defensive fallback — chat may have persisted a current_node
        # that doesn't exist in this template version. Falling back to
        # initial keeps the bot functional; we log loudly so operators
        # notice template drift.
        logger.warning(
            f"prepare_resume_node: start_node {node_name!r} not in flow; "
            f"falling back to initial_node {flow_config['initial_node']!r}"
        )
        node_name = flow_config["initial_node"]

    node_config = flow_config["nodes"][node_name]

    role_messages = inject_language_rules(
        node_config.get("role_messages", []),
        lead_payload.get("language_name", "English"),
        getattr(configurations, "payload_based_language_selection", False),
    )

    task_messages: List[Dict[str, Any]] = list(node_config["task_messages"])
    # Tail-append prior history so the RESET frame's payload becomes
    # [role + task + history] in a single context replacement. Filter
    # out any malformed entries defensively.
    for entry in prior_history or []:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        content = entry.get("content")
        if role not in ("user", "assistant") or not content:
            continue
        task_messages.append({"role": role, "content": content})

    logger.info(
        f"prepare_resume_node: resuming at {node_name!r} with "
        f"{len(prior_history or [])} prior messages"
    )

    return NodeConfig(
        name=node_config["name"],
        task_messages=task_messages,
        role_messages=role_messages,
        functions=node_config.get("functions", []),
        pre_actions=node_config.get("pre_actions", []),
        post_actions=node_config.get("post_actions", []),
        # No greeting on resume — let the user speak first.
        respond_immediately=True,
    )

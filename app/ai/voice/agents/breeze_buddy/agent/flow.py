"""Flow management and node configuration for voice agents."""

from typing import Any, Dict, List, Optional, cast

from pipecat.services.azure.llm import AzureLLMService
from pipecat_flows import FlowManager, NodeConfig
from pipecat_flows.types import FlowsDirectFunction, FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.services.knowledge_base import (
    build_kb_system_message,
)
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.loader import FlowConfigLoader
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    TemplateModel,
)
from app.ai.voice.agents.breeze_buddy.template.utils import validate_template_compat
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
        template_id=lead.template_id,
        call_payload=lead.payload,
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
    # NOTE: generative voice UI (the ``{{ui_primitives_section}}`` prompt
    # splice) is deferred — see docs/widget/VOICE_GENERATIVE_UI_TODO.md. The
    # builder leaves the placeholder inert when no ui_allowlist is passed.
    flow_config = flow_builder.build_flow_config(template)
    end_conversation_callbacks = flow_config.get("end_conversation_callbacks", [])
    expected_callback_response_schema = flow_config.get(
        "expected_callback_response_schema", None
    )

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
    kb_text: Optional[str] = None,
) -> NodeConfig:
    """Prepare the initial node configuration with language injection.

    Args:
        flow_config: The flow configuration dict
        lead_payload: Lead payload data
        configurations: Template configurations
        has_greeting_source: Whether a greeting source exists
        greeting_text: The resolved greeting text that was played to the user
        kb_text: Full knowledge base text (full_injection mode). Injected once
            into the initial node's role_messages; pipecat-flows transitions
            APPEND, so it persists across all nodes. Appended at the tail so
            the static prompt prefix stays cache-friendly.

    Returns:
        Configured NodeConfig for the initial node
    """
    initial_node_name = flow_config["initial_node"]
    node_config = flow_config["nodes"][initial_node_name]

    stt_config = getattr(configurations, "stt_configuration", None)
    if stt_config is not None:
        stt_config.payload_based_language_selection
    else:
        getattr(configurations, "payload_based_language_selection", False)

    role_messages = inject_language_rules(
        node_config.get("role_messages", []),
        lead_payload.get("language_name", "English"),
        getattr(configurations, "payload_based_language_selection", False),
    )

    if kb_text and configurations and configurations.knowledge_base:
        role_messages = list(role_messages or []) + [
            build_kb_system_message(kb_text, configurations.knowledge_base)
        ]
        logger.info(f"Injected full KB text into initial node ({len(kb_text)} chars)")

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

    return NodeConfig(
        name=node_config["name"],
        task_messages=task_messages,
        role_messages=role_messages,
        functions=node_config.get("functions", []),
        pre_actions=node_config.get("pre_actions", []),
        post_actions=node_config.get("post_actions", []),
        respond_immediately=not has_greeting_source,
    )

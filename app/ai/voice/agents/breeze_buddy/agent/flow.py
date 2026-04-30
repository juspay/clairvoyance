"""Flow management and node configuration for voice agents."""

from typing import Any, Dict, List, Optional, cast

from pipecat.services.azure.llm import AzureLLMService
from pipecat_flows import FlowManager, NodeConfig
from pipecat_flows.types import FlowsDirectFunction, FlowsFunctionSchema

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
from app.schemas.breeze_buddy.core import LeadCallTracker


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
    )

    # Apply overrides and re-render if playground mode
    template = apply_playground_overrides(lead, template, template_vars)

    return template, template.configurations, template_vars


def setup_flow_manager(
    task: Any,
    llm: AzureLLMService,
    context_aggregator: Any,
    transport: Any,
    flow_builder: FlowConfigBuilder,
    template: TemplateModel,
    bot_instance: Any = None,
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

    Returns:
        Configured FlowManager
    """
    global_functions = flow_builder.build_global_functions(
        flow=template.flow, bot_instance=bot_instance
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

    return NodeConfig(
        name=node_config["name"],
        task_messages=task_messages,
        role_messages=role_messages,
        functions=node_config.get("functions", []),
        pre_actions=node_config.get("pre_actions", []),
        post_actions=node_config.get("post_actions", []),
        respond_immediately=not has_greeting_source,
    )

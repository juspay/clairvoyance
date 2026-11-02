from typing import Any, cast

import pytest
from pipecat.adapters.llm.services.anthropic_adapter import AnthropicLLMAdapter
from pipecat.adapters.llm.services.bedrock_adapter import AWSBedrockLLMAdapter
from pipecat.adapters.llm.services.gemini_adapter import GeminiLLMAdapter
from pipecat.flows import FlowError, FlowManager, NodeConfig
from pipecat.flows.types import FlowsFunctionSchema
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openai.llm import OpenAILLMService

from app.ai.voice.agents.breeze_buddy.agent.flow import (
    OPENING_TURN_MESSAGE,
    BuddyFlowManager,
    build_flow_config,
    prepare_initial_node,
)
from app.ai.voice.agents.breeze_buddy.agent.prompt_prefill import (
    prefill_system_instruction,
)
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    KnowledgeBaseConfig,
    TemplateModel,
)

PERSONA = "You are Ravi, a support agent from Test Store."
STYLE = "Speak natural Hinglish."
TASK = "Greet Rohit and ask if this is a good time to talk."
KB_TEXT = "Returns are accepted within 7 days of delivery."


class _Builder:
    def build_flow_config(self, template: Any) -> dict:
        return {
            "initial_node": "greet",
            "nodes": {
                "greet": {
                    "name": "greet",
                    "role_messages": [
                        {"role": "system", "content": PERSONA},
                        {"role": "system", "content": STYLE},
                    ],
                    "task_messages": [{"role": "system", "content": TASK}],
                    "functions": [],
                }
            },
        }


def flow_for(llm: Any) -> dict:
    flow_config, _, _ = build_flow_config(
        cast(FlowConfigBuilder, _Builder()), cast(TemplateModel, None), llm=llm
    )
    return flow_config


def test_the_persona_becomes_the_role_message_and_openai_tasks_stay_system():
    node = flow_for(OpenAILLMService(api_key="test", model="gpt-4.1"))["nodes"]["greet"]

    assert "role_messages" not in node
    assert node["role_message"] == f"{PERSONA}\n\n{STYLE}"
    assert node["task_messages"] == [{"role": "system", "content": TASK}]


def test_tasks_are_developer_messages_for_an_llm_that_takes_the_system_apart():
    node = flow_for(GoogleLLMService(api_key="test"))["nodes"]["greet"]

    assert node["role_message"] == f"{PERSONA}\n\n{STYLE}"
    assert node["task_messages"] == [{"role": "developer", "content": TASK}]


@pytest.mark.parametrize(
    "adapter", [GeminiLLMAdapter(), AnthropicLLMAdapter(), AWSBedrockLLMAdapter()]
)
async def test_the_model_gets_both_the_persona_and_the_task(adapter):
    node = flow_for(GoogleLLMService(api_key="test"))["nodes"]["greet"]
    context = LLMContext(
        messages=[*node["task_messages"], {"role": "user", "content": "Haan, boliye"}]
    )
    kwargs: dict = {"system_instruction": node["role_message"]}
    if isinstance(adapter, AnthropicLLMAdapter):
        kwargs["enable_prompt_caching"] = False

    params = await adapter.get_llm_invocation_params(context, **kwargs)

    system = params.get("system") or params.get("system_instruction")
    assert PERSONA in str(system)
    assert TASK in str(params["messages"])


async def test_a_system_message_first_in_the_context_is_dropped_beside_a_system_instruction():
    context = LLMContext(
        messages=[
            {"role": "system", "content": PERSONA},
            {"role": "user", "content": "Haan, boliye"},
        ]
    )

    params = await GeminiLLMAdapter().get_llm_invocation_params(
        context, system_instruction="ASYNC TOOLS"
    )

    assert PERSONA not in str(params)


def test_the_kb_message_takes_the_task_role_and_leads_the_initial_node():
    llm = GoogleLLMService(api_key="test")
    configurations = ConfigurationModel(
        knowledge_base=KnowledgeBaseConfig(
            knowledge_base_ids=["kb-1"], mode="full_injection"
        )
    )

    node = prepare_initial_node(
        flow_config=flow_for(llm),
        lead_payload={},
        configurations=configurations,
        has_greeting_source=True,
        greeting_text="Hello Rohit",
        kb_text=KB_TEXT,
        llm=llm,
    )

    kb, greeting, task = node["task_messages"]
    assert kb["role"] == "developer" and KB_TEXT in kb["content"]
    assert greeting == {"role": "assistant", "content": "Hello Rohit"}
    assert task == {"role": "developer", "content": TASK}
    assert node["role_message"].startswith(PERSONA)


def test_a_realtime_llm_keeps_the_persona_and_the_kb_in_the_context():
    llm = GeminiLiveLLMService(api_key="test")
    flow_config = flow_for(llm)
    configurations = ConfigurationModel(
        knowledge_base=KnowledgeBaseConfig(
            knowledge_base_ids=["kb-1"], mode="full_injection"
        )
    )

    node = prepare_initial_node(
        flow_config=flow_config,
        lead_payload={},
        configurations=configurations,
        has_greeting_source=False,
        kb_text=KB_TEXT,
        llm=llm,
    )

    assert "role_message" not in flow_config["nodes"]["greet"]
    assert "role_message" not in node
    persona, style, kb = node["role_messages"]
    assert persona == {"role": "system", "content": PERSONA}
    assert style == {"role": "system", "content": STYLE}
    assert kb["role"] == "system" and KB_TEXT in kb["content"]
    assert node["task_messages"] == [{"role": "system", "content": TASK}]


@pytest.mark.parametrize("cancel_on_interruption", [False, True])
async def test_the_prefill_warms_the_system_text_pipecat_sends(cancel_on_interruption):
    async def handler(params):
        pass

    llm = OpenAILLMService(
        api_key="test",
        settings=OpenAILLMService.Settings(model="gpt-4.1", system_instruction=PERSONA),
    )
    llm.register_function(
        "order_confirmed", handler, cancel_on_interruption=cancel_on_interruption
    )
    llm._compose_system_instruction()
    entry = FlowsFunctionSchema(
        name="order_confirmed",
        description="Confirm the order.",
        properties={},
        required=[],
        handler=handler,
        cancel_on_interruption=cancel_on_interruption,
    )

    assert (
        prefill_system_instruction(PERSONA, [entry]) == llm._settings.system_instruction
    )


class _TwoNodeBuilder:
    def build_flow_config(self, template: Any) -> dict:
        return {
            "initial_node": "greet",
            "nodes": {
                "greet": {
                    "name": "greet",
                    "role_messages": [{"role": "system", "content": PERSONA}],
                    "task_messages": [{"role": "system", "content": TASK}],
                },
                "confirm": {
                    "name": "confirm",
                    "role_messages": [{"role": "system", "content": STYLE}],
                    "task_messages": [{"role": "system", "content": "Confirm it."}],
                },
            },
        }


def _two_node_flow(llm: Any) -> dict:
    flow_config, _, _ = build_flow_config(
        cast(FlowConfigBuilder, _TwoNodeBuilder()), cast(TemplateModel, object()), llm
    )
    return flow_config


def test_only_the_initial_node_sets_the_system_instruction():
    llm = GoogleLLMService(api_key="test", model="gemini-2.5-flash")
    nodes = _two_node_flow(llm)["nodes"]
    assert nodes["greet"]["role_message"] == PERSONA
    assert "role_message" not in nodes["confirm"]
    assert nodes["confirm"]["task_messages"] == [
        {"role": "developer", "content": STYLE},
        {"role": "developer", "content": "Confirm it."},
    ]


def test_openai_keeps_a_later_nodes_role_messages_as_system():
    llm = OpenAILLMService(api_key="test", model="gpt-4.1")
    nodes = _two_node_flow(llm)["nodes"]
    assert "role_message" not in nodes["confirm"]
    assert nodes["confirm"]["task_messages"][0] == {"role": "system", "content": STYLE}


def test_language_rules_stay_when_the_call_reenters_the_initial_node():
    llm = OpenAILLMService(api_key="test", model="gpt-4.1")
    flow_config = _two_node_flow(llm)
    node = prepare_initial_node(
        flow_config=flow_config,
        lead_payload={"language_name": "Telugu"},
        configurations=ConfigurationModel(payload_based_language_selection=True),
        has_greeting_source=False,
        greeting_text=None,
        kb_text=None,
        llm=llm,
    )
    assert "Telugu" in node["role_message"]
    assert flow_config["nodes"]["greet"]["role_message"] == node["role_message"]


class _NoTaskBuilder:
    def build_flow_config(self, template: Any) -> dict:
        return {
            "initial_node": "direct",
            "nodes": {
                "direct": {
                    "name": "direct",
                    "role_messages": [{"role": "system", "content": PERSONA}],
                    "task_messages": [],
                }
            },
        }


def _no_task_node(llm: Any, has_greeting: bool) -> Any:
    flow_config, _, _ = build_flow_config(
        cast(FlowConfigBuilder, _NoTaskBuilder()), cast(TemplateModel, object()), llm
    )
    return prepare_initial_node(
        flow_config=flow_config,
        lead_payload={},
        configurations=ConfigurationModel(),
        has_greeting_source=has_greeting,
        greeting_text="Namaste" if has_greeting else None,
        kb_text=None,
        llm=llm,
    )


def test_an_opening_turn_with_no_task_still_has_a_message_to_send():
    node = _no_task_node(
        GoogleLLMService(api_key="test", model="gemini-2.5-flash"), False
    )
    assert node["respond_immediately"] is True
    assert node["task_messages"] == [
        {"role": "developer", "content": OPENING_TURN_MESSAGE}
    ]


def test_openai_and_a_played_greeting_need_no_opening_message():
    openai_node = _no_task_node(
        OpenAILLMService(api_key="test", model="gpt-4.1"), False
    )
    assert openai_node["task_messages"] == []
    greeted = _no_task_node(
        GoogleLLMService(api_key="test", model="gemini-2.5-flash"), True
    )
    assert greeted["task_messages"] == [{"role": "assistant", "content": "Namaste"}]


def test_the_flow_manager_leaves_double_braces_in_a_prompt_alone(monkeypatch):
    node = cast(
        NodeConfig,
        {
            "name": "greet",
            "role_message": "You help {{customer}}.",
            "task_messages": [{"role": "system", "content": "Ask about {{order}}."}],
        },
    )
    monkeypatch.setattr(FlowManager, "state", {})
    manager = BuddyFlowManager.__new__(BuddyFlowManager)

    assert manager._render_node("greet", node) is node
    with pytest.raises(FlowError):
        FlowManager._render_node(manager, "greet", node)

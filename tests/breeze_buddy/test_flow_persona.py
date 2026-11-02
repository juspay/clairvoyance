from typing import Any, cast

import pytest
from pipecat.adapters.llm.services.anthropic_adapter import AnthropicLLMAdapter
from pipecat.adapters.llm.services.bedrock_adapter import AWSBedrockLLMAdapter
from pipecat.adapters.llm.services.gemini_adapter import GeminiLLMAdapter
from pipecat.flows.types import FlowsFunctionSchema
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openai.llm import OpenAILLMService

from app.ai.voice.agents.breeze_buddy.agent.flow import (
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

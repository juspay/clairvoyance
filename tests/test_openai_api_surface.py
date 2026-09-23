"""OpenAI provider path: /chat/completions vs /v1/responses selection and the
request shape each surface needs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.responses.llm import OpenAIResponsesHttpLLMService
from pipecat_flows.types import FlowsFunctionSchema

import app.ai.voice.agents.breeze_buddy.llm as resolver_mod
from app.ai.voice.agents.breeze_buddy.agent import prompt_prefill
from app.ai.voice.agents.breeze_buddy.llm import _resolve_openai
from app.ai.voice.llm import (
    LLMConfiguration,
    LLMProvider,
    OpenAIConfig,
    ThinkingConfiguration,
    build_openai_responses_llm,
    uses_responses_surface,
)

BEDROCK = "https://bedrock-runtime.ap-south-1.amazonaws.com/openai/v1"
GRID = "https://grid.juspay.in/v1"


# ---------------------------------------------------------------------------
# surface selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url, expected",
    [
        (BEDROCK, True),
        ("https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1", True),
        (GRID, False),
        (None, False),
        ("https://api.openai.com/v1", False),
        # a path that merely mentions bedrock is not the Bedrock host
        ("https://grid.juspay.in/bedrock/v1", False),
    ],
)
def test_uses_responses_surface(base_url, expected):
    assert uses_responses_surface(base_url) is expected


# ---------------------------------------------------------------------------
# responses builder request shape
# ---------------------------------------------------------------------------


def _cfg(**over: Any) -> OpenAIConfig:
    base: dict[str, Any] = dict(api_key="k", model="m", base_url=BEDROCK)
    base.update(over)
    return OpenAIConfig(**base)


def test_responses_builder_thinking_on_is_nested_reasoning():
    svc = build_openai_responses_llm(_cfg(reasoning_effort="low"))
    assert isinstance(svc, OpenAIResponsesHttpLLMService)
    assert svc._settings.extra == {"reasoning": {"effort": "low"}}
    assert svc._settings.model == "m"


def test_responses_builder_thinking_off_is_effort_none_not_chat_template_kwargs():
    svc = build_openai_responses_llm(_cfg(disable_thinking=True))
    assert svc._settings.extra == {"reasoning": {"effort": "none"}}
    assert "extra_body" not in svc._settings.extra


def test_responses_builder_no_thinking_block_sends_no_reasoning():
    svc = build_openai_responses_llm(_cfg())
    assert "reasoning" not in svc._settings.extra


def test_responses_builder_extra_body_stays_nested_and_tool_choice_passes():
    svc = build_openai_responses_llm(
        _cfg(
            tool_choice="required",
            extra_body={"prompt_cache_key": "abc", "parallel_tool_calls": False},
        )
    )
    assert svc._settings.extra["tool_choice"] == "required"
    assert svc._settings.extra["extra_body"] == {
        "prompt_cache_key": "abc",
        "parallel_tool_calls": False,
    }
    # never spread as SDK kwargs — an unknown key would be a TypeError
    assert "prompt_cache_key" not in svc._settings.extra


def test_responses_builder_temperature_only_when_set():
    svc = build_openai_responses_llm(_cfg())
    params = svc._build_response_params({"input": [], "tools": []})
    assert "temperature" not in params
    assert "max_output_tokens" not in params

    svc = build_openai_responses_llm(_cfg(temperature=1.0, max_tokens=500))
    params = svc._build_response_params({"input": [], "tools": []})
    assert params["temperature"] == 1.0
    assert params["max_output_tokens"] == 500
    assert params["store"] is False


# ---------------------------------------------------------------------------
# resolver gate
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_config(monkeypatch):
    async def fake_get_config(key, default, _type=str):
        return "secret" if key == "BEDROCK_API_KEY" else default

    async def fake_temperature():
        return 0.7

    async def fake_max_tokens():
        return 300

    monkeypatch.setattr(resolver_mod, "get_config", fake_get_config)
    monkeypatch.setattr(resolver_mod, "OPENAI_TEMPERATURE", fake_temperature)
    monkeypatch.setattr(resolver_mod, "OPENAI_MAX_COMPLETION_TOKENS", fake_max_tokens)


def _llm_config(**over: Any) -> LLMConfiguration:
    base: dict[str, Any] = dict(
        provider=LLMProvider.OPENAI,
        model="in.openai.gpt-5.6-luna",
        endpoint=BEDROCK,
        api_key_name="BEDROCK_API_KEY",
        max_tokens=500,
    )
    base.update(over)
    return LLMConfiguration(**base)


def test_resolver_bedrock_thinking_on_uses_responses(stub_config):
    svc = asyncio.run(
        _resolve_openai(
            _llm_config(
                temperature=1,
                thinking=ThinkingConfiguration(enabled=True, reasoning_effort="low"),
            )
        )
    )
    assert isinstance(svc, OpenAIResponsesHttpLLMService)
    assert svc._settings.extra["reasoning"] == {"effort": "low"}


def test_resolver_bedrock_thinking_off_still_uses_responses_with_effort_none(
    stub_config,
):
    svc = asyncio.run(
        _resolve_openai(_llm_config(thinking=ThinkingConfiguration(enabled=False)))
    )
    assert isinstance(svc, OpenAIResponsesHttpLLMService)
    assert svc._settings.extra["reasoning"] == {"effort": "none"}


def test_resolver_bedrock_no_thinking_block_uses_responses(stub_config):
    svc = asyncio.run(_resolve_openai(_llm_config()))
    assert isinstance(svc, OpenAIResponsesHttpLLMService)
    assert "reasoning" not in svc._settings.extra


def test_resolver_bedrock_never_injects_dynamic_temperature(stub_config):
    svc = asyncio.run(_resolve_openai(_llm_config()))
    assert isinstance(svc, OpenAIResponsesHttpLLMService)
    params = svc._build_response_params({"input": [], "tools": []})
    assert "temperature" not in params
    assert params["max_output_tokens"] == 500


def test_resolver_gateway_keeps_chat_completions_and_dynamic_temperature(
    stub_config,
):
    svc = asyncio.run(
        _resolve_openai(
            _llm_config(
                endpoint=GRID,
                api_key_name="BEDROCK_API_KEY",
                thinking=ThinkingConfiguration(enabled=True, reasoning_effort="low"),
            )
        )
    )
    assert isinstance(svc, OpenAILLMService)
    assert svc._settings.temperature == 0.7
    assert svc._settings.extra["reasoning_effort"] == "low"


# ---------------------------------------------------------------------------
# prefill on the responses surface
# ---------------------------------------------------------------------------


def _responses_service(**over: Any) -> OpenAIResponsesHttpLLMService:
    return build_openai_responses_llm(
        _cfg(reasoning_effort="low", extra_body={"prompt_cache_key": "k"}, **over)
    )


def _capture_client(response: Any, calls: list) -> Any:
    async def create(**params):
        calls.append(params)
        return response

    return SimpleNamespace(responses=SimpleNamespace(create=create))


def test_prefill_gate_admits_responses_service(monkeypatch):
    spawned: list = []

    def fake_spawn(coro, name=None):
        coro.close()
        spawned.append(name)
        return object()

    monkeypatch.setattr(prompt_prefill, "spawn_background_task", fake_spawn)
    llm_config = LLMConfiguration(
        provider=LLMProvider.OPENAI, endpoint=BEDROCK, prefill_system_prompt=True
    )
    task = prompt_prefill.spawn_prefill(
        llm_service=_responses_service(),
        llm_config=llm_config,
        initial_node_config={"role_messages": [{"role": "system", "content": "x"}]},
        global_functions=[],
    )
    assert task is not None
    assert spawned == ["prompt-cache-prefill"]


def test_prefill_responses_request_is_prefix_parity_plus_synthetic_turn():
    svc = _responses_service(max_tokens=500)
    calls: list = []
    usage = SimpleNamespace(
        input_tokens=7562,
        input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=7560),
    )
    svc._client = _capture_client(SimpleNamespace(usage=usage), calls)

    async def handler(args: dict, flow_manager: Any) -> Any:
        return None

    fn = FlowsFunctionSchema(
        name="user_busy",
        description="busy",
        properties={"callback_note": {"type": "string"}},
        required=["callback_note"],
        handler=handler,
    )
    asyncio.run(
        prompt_prefill.prefill_system_prompt(
            llm_service=svc,
            messages=[
                {"role": "system", "content": "manual"},
                {"role": "system", "content": "lead data"},
                {"role": "assistant", "content": "greeting"},
            ],
            function_entries=[fn],
        )
    )
    assert len(calls) == 1
    params = calls[0]
    assert params["stream"] is False
    assert params["max_output_tokens"] == prompt_prefill._PREFILL_MAX_COMPLETION_TOKENS
    assert params["store"] is False
    assert params["reasoning"] == {"effort": "low"}
    assert params["extra_body"] == {"prompt_cache_key": "k"}
    assert [t["name"] for t in params["tools"]] == ["user_busy"]
    # the warmed prefix: both system messages as developer turns + greeting,
    # then the synthetic user turn AFTER it
    assert [m["role"] for m in params["input"]] == [
        "developer",
        "developer",
        "assistant",
        "user",
    ]
    assert params["input"][-1]["content"] == prompt_prefill._PREFILL_SYNTHETIC_USER_TURN
    # the settings' completion budget must not leak into the prefill
    assert params["max_output_tokens"] != 500


# ---------------------------------------------------------------------------
# observers on the responses surface
# ---------------------------------------------------------------------------


def test_observer_call_llm_dispatches_responses_service():
    from pipecat.adapters.schemas.function_schema import FunctionSchema

    from app.ai.voice.agents.breeze_buddy.observers.llm import call_llm

    svc = build_openai_responses_llm(
        _cfg(reasoning_effort="low", tool_choice="required")
    )
    calls: list = []
    output = [
        SimpleNamespace(type="reasoning"),
        SimpleNamespace(
            type="function_call", name="hold_detected", arguments='{"reason": "music"}'
        ),
    ]
    svc._client = _capture_client(SimpleNamespace(output=output), calls)

    tool = FunctionSchema(
        name="hold_detected",
        description="hold",
        properties={"reason": {"type": "string"}},
        required=["reason"],
    )
    name, args = asyncio.run(
        call_llm(svc, "please hold", "you are an observer", [tool], "hold")
    )
    assert (name, args) == ("hold_detected", {"reason": "music"})

    params = calls[0]
    assert params["stream"] is False
    assert params["reasoning"] == {"effort": "low"}
    # the conversation's forced tool_choice must not bind the observer
    assert params["tool_choice"] == "auto"
    assert [t["name"] for t in params["tools"]] == ["hold_detected"]
    assert params["input"][-1] == {"role": "user", "content": "please hold"}


def test_observer_call_llm_responses_no_tool_call():
    from app.ai.voice.agents.breeze_buddy.observers.llm import call_llm

    svc = build_openai_responses_llm(_cfg())
    svc._client = _capture_client(SimpleNamespace(output=[]), [])
    assert asyncio.run(call_llm(svc, "hi", "sys", [], "x")) == (None, None)


def test_observer_merge_skips_temperature_default_on_bedrock():
    from app.ai.voice.agents.breeze_buddy.observers.factory import merge_llm_config

    bedrock = merge_llm_config(None, _llm_config())
    assert bedrock.temperature is None
    assert bedrock.max_tokens == 256

    explicit = merge_llm_config(LLMConfiguration(temperature=1), _llm_config())
    assert explicit.temperature == 1

    gateway = merge_llm_config(None, _llm_config(endpoint=GRID))
    assert gateway.temperature == 0.1

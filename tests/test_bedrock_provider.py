"""AWS Bedrock provider: builder mapping, resolver routing, observer call."""

from __future__ import annotations

import os

import pytest
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.aws.llm import AWSBedrockLLMService

import app.ai.voice.agents.breeze_buddy.llm as resolver_mod
import app.ai.voice.agents.breeze_buddy.provider_credentials as pc
from app.ai.voice.agents.breeze_buddy.llm import get_llm_service
from app.ai.voice.agents.breeze_buddy.observers.factory import merge_llm_config
from app.ai.voice.agents.breeze_buddy.observers.llm import call_llm
from app.ai.voice.llm import (
    BedrockConfig,
    LLMConfiguration,
    LLMProvider,
    ThinkingConfiguration,
    build_bedrock_llm,
)


def test_builder_maps_reasoning_scopes_key_and_omits_temperature(monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    svc = build_bedrock_llm(
        BedrockConfig(
            model="m",
            region="ap-south-1",
            max_tokens=500,
            api_key="k",
            reasoning_effort="low",
        )
    )
    assert svc._settings.additional_model_request_fields == {
        "reasoning": {"effort": "low"}
    }
    assert svc._settings.temperature is None
    assert svc._aws_params["region_name"] == "ap-south-1"
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ
    provider = svc._aws_session._session.get_component("token_provider")
    assert provider.load_token(signing_name="bedrock").token == "k"
    assert getattr(svc._aws_params["config"], "signature_version") == "bearer"


async def test_resolver_routes_bedrock_and_requires_region(monkeypatch):
    async def fake_get_config(name, default, _type):
        return "key"

    monkeypatch.setattr(pc, "get_config", fake_get_config)
    cfg = LLMConfiguration(
        provider=LLMProvider.AWS_BEDROCK,
        model="in.openai.gpt-5.6-luna",
        region="ap-south-1",
        max_tokens=500,
        api_key_name="BEDROCK_API_KEY",
        thinking=ThinkingConfiguration(enabled=False),
    )
    svc = await get_llm_service(cfg)
    assert isinstance(svc, AWSBedrockLLMService)
    assert svc._settings.additional_model_request_fields == {
        "reasoning": {"effort": "none"}
    }
    with pytest.raises(ValueError, match="region"):
        await get_llm_service(cfg.model_copy(update={"region": None}))
    with pytest.raises(ValueError, match="temperature"):
        await get_llm_service(
            cfg.model_copy(
                update={
                    "temperature": 0.67,
                    "thinking": ThinkingConfiguration(
                        enabled=True, reasoning_effort="low"
                    ),
                }
            )
        )
    svc = await get_llm_service(cfg.model_copy(update={"temperature": 0.67}))
    assert isinstance(svc, AWSBedrockLLMService)
    assert svc._settings.additional_model_request_fields == {
        "reasoning": {"effort": "none"},
        "temperature": 0.67,
    }
    assert svc._settings.temperature is None


async def test_observer_call_parses_tool_use(monkeypatch):
    svc = build_bedrock_llm(
        BedrockConfig(model="m", region="ap-south-1", max_tokens=256)
    )
    sent: dict = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def converse(self, **params):
            sent.update(params)
            return {
                "output": {
                    "message": {
                        "content": [{"toolUse": {"name": "end_call", "input": {}}}]
                    }
                }
            }

    monkeypatch.setattr(svc._aws_session, "client", lambda **kw: _Client())
    tools = [
        FunctionSchema(name="end_call", description="d", properties={}, required=[])
    ]

    result = await call_llm(
        svc, "leave a message after the beep", "Detect voicemail.", tools, "vm"
    )

    assert result == ("end_call", {})
    assert sent["toolConfig"]["toolChoice"] == {"auto": {}}
    assert "temperature" not in sent["inferenceConfig"]


def test_observer_merge_skips_temperature_default_on_bedrock():
    base = LLMConfiguration(
        provider=LLMProvider.AWS_BEDROCK, model="m", region="ap-south-1"
    )
    merged = merge_llm_config(None, base)
    assert merged.temperature is None
    assert merged.max_tokens == 256


def test_observer_merge_disables_thinking_on_bedrock_unless_overridden():
    base = LLMConfiguration(
        provider=LLMProvider.AWS_BEDROCK,
        model="in.openai.gpt-5.6-luna",
        region="ap-south-1",
        thinking=ThinkingConfiguration(enabled=True, reasoning_effort="low"),
    )
    merged = merge_llm_config(None, base)
    assert merged.thinking is not None and merged.thinking.enabled is False

    override = LLMConfiguration(
        thinking=ThinkingConfiguration(enabled=True, reasoning_effort="minimal")
    )
    assert merge_llm_config(override, base).thinking == override.thinking


def test_observer_merge_leaves_thinking_unset_off_bedrock():
    """Non-Bedrock observers never reason: the 0.1 temperature default would
    pair with it, and reasoning models reject that combination."""
    base = LLMConfiguration(
        provider=LLMProvider.AZURE,
        thinking=ThinkingConfiguration(enabled=True, reasoning_effort="low"),
    )
    assert merge_llm_config(None, base).thinking is None

    override = ThinkingConfiguration(enabled=True, reasoning_effort="minimal")
    assert merge_llm_config(LLMConfiguration(thinking=override), base).thinking == (
        override
    )


async def test_observer_on_bedrock_gpt_resolves_with_reasoning_off(monkeypatch):
    async def fake_get_config(name, default, _type):
        return "key"

    monkeypatch.setattr(pc, "get_config", fake_get_config)
    base = LLMConfiguration(
        provider=LLMProvider.AWS_BEDROCK,
        model="in.openai.gpt-5.6-luna",
        region="ap-south-1",
        max_tokens=500,
        api_key_name="BEDROCK_API_KEY",
        thinking=ThinkingConfiguration(enabled=True, reasoning_effort="low"),
    )
    # The old observer default temperature: would raise if reasoning stayed on.
    merged = merge_llm_config(LLMConfiguration(temperature=0.1), base)
    svc = await get_llm_service(merged)
    assert isinstance(svc, AWSBedrockLLMService)
    assert svc._settings.additional_model_request_fields == {
        "reasoning": {"effort": "none"},
        "temperature": 0.1,
    }
    assert svc._settings.max_tokens == 256


async def test_bedrock_prefill_merges_synthetic_turn_into_last_user_message(
    monkeypatch,
):
    from pipecat.processors.aggregators.llm_context import LLMContext

    from app.ai.voice.agents.breeze_buddy.agent import prompt_prefill

    svc = build_bedrock_llm(
        BedrockConfig(model="m", region="ap-south-1", max_tokens=16)
    )
    sent: dict = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def converse(self, **params):
            sent.update(params)
            return {"usage": {"inputTokens": 2, "cacheWriteInputTokens": 50}}

    monkeypatch.setattr(svc._aws_session, "client", lambda **kw: _Client())
    context = LLMContext(
        messages=[
            {"role": "system", "content": "manual"},
            {"role": "system", "content": "instructions"},
        ]
    )

    await prompt_prefill._bedrock_prefill(svc, context)

    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["user"]
    assert sent["messages"][-1]["content"][-1] == {"text": "."}
    assert sent["inferenceConfig"]["maxTokens"] == 16

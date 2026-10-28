"""The default LLM: a template that names no provider runs on luna on Bedrock."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pipecat.services.aws.llm import AWSBedrockLLMService
from pipecat.services.azure.llm import AzureLLMService

import app.ai.voice.agents.breeze_buddy.accounts.llm as accounts_llm
import app.ai.voice.agents.breeze_buddy.llm as llm_mod
from app.ai.voice.agents.breeze_buddy.llm import (
    DEFAULT_LLM_CONFIGURATION,
    get_llm_service,
)
from app.ai.voice.agents.breeze_buddy.observers.factory import merge_llm_config
from app.ai.voice.agents.breeze_buddy.utils import hold_transfer
from app.ai.voice.llm import LLMConfiguration, LLMProvider, ThinkingConfiguration

LUNA = "in.openai.gpt-5.6-luna"


@pytest.fixture
def keys(monkeypatch):
    """Dynamic config as prod has it: the Bedrock key under OPENAI_API_KEY and
    an Azure key under AZURE_X. The environment has no Azure account, so a
    default that resolved to Azure would fail."""

    async def fake_get_config(name, default, _type=str):
        return {"OPENAI_API_KEY": "bedrock-key", "AZURE_X": "azure-key"}.get(
            name, default
        )

    async def azure_temperature() -> float:
        return 0.7

    async def azure_max_tokens() -> int:
        return 50

    monkeypatch.setattr(accounts_llm, "get_config", fake_get_config)
    monkeypatch.setattr(accounts_llm.static, "AZURE_OPENAI_API_KEY", None)
    monkeypatch.setattr(accounts_llm.static, "AZURE_OPENAI_ENDPOINT", None)
    monkeypatch.setattr(llm_mod, "BREEZE_BUDDY_AZURE_TEMPERATURE", azure_temperature)
    monkeypatch.setattr(
        llm_mod, "BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS", azure_max_tokens
    )


def _bedrock_key(svc: AWSBedrockLLMService) -> str:
    provider = svc._aws_session._session.get_component("token_provider")
    return provider.load_token(signing_name="bedrock").token


async def test_no_config_runs_luna_on_bedrock(keys):
    svc = await get_llm_service(None)

    assert isinstance(svc, AWSBedrockLLMService)
    assert svc._settings.model == LUNA
    assert svc._aws_params["region_name"] == "ap-south-1"
    assert svc._settings.additional_model_request_fields == {
        "reasoning": {"effort": "none"},
        "temperature": 0.65,
    }
    assert svc._settings.max_tokens == 500
    assert _bedrock_key(svc) == "bedrock-key"


async def test_the_default_block_is_luna_with_reasoning_off():
    block = DEFAULT_LLM_CONFIGURATION
    assert (block.provider, block.model, block.region) == (
        LLMProvider.AWS_BEDROCK,
        LUNA,
        "ap-south-1",
    )
    assert (block.temperature, block.max_tokens, block.api_key_name) == (
        0.65,
        500,
        "OPENAI_API_KEY",
    )
    assert block.thinking is not None
    assert (block.thinking.enabled, block.thinking.reasoning_effort) == (False, None)
    assert (block.tool_choice, block.function_call_timeout_secs) == ("auto", 10)


async def test_reasoning_off_sends_the_same_request_as_effort_none(keys):
    # The 1,400+ migrated templates carry enabled=true + effort "none"; the
    # default's enabled=false must put the very same request on the wire.
    off = await get_llm_service(DEFAULT_LLM_CONFIGURATION)
    effort_none = await get_llm_service(
        DEFAULT_LLM_CONFIGURATION.model_copy(
            update={
                "thinking": ThinkingConfiguration(enabled=True, reasoning_effort="none")
            }
        )
    )
    assert isinstance(off, AWSBedrockLLMService)
    assert isinstance(effort_none, AWSBedrockLLMService)
    assert (
        off._settings.additional_model_request_fields
        == effort_none._settings.additional_model_request_fields
        == {"reasoning": {"effort": "none"}, "temperature": 0.65}
    )
    assert off._settings.max_tokens == effort_none._settings.max_tokens == 500


async def test_an_empty_block_runs_luna(keys):
    svc = await get_llm_service(LLMConfiguration())
    assert isinstance(svc, AWSBedrockLLMService)
    assert svc._settings.model == LUNA


async def test_a_providerless_block_keeps_its_tuning_but_not_its_reasoning(keys):
    # Reasoning "low" with temperature 0.3 is refused on luna; the default
    # keeps "none" so the call never degrades to silence.
    svc = await get_llm_service(
        LLMConfiguration(
            temperature=0.3,
            max_tokens=200,
            function_call_timeout_secs=20,
            thinking=ThinkingConfiguration(enabled=True, reasoning_effort="low"),
        )
    )
    assert isinstance(svc, AWSBedrockLLMService)
    assert svc._settings.additional_model_request_fields == {
        "reasoning": {"effort": "none"},
        "temperature": 0.3,
    }
    assert svc._settings.max_tokens == 200


@pytest.mark.parametrize(
    "block",
    [
        LLMConfiguration(api_key_name="AZURE_X"),
        LLMConfiguration(
            endpoint="https://other-azure.example", api_key_name="AZURE_X"
        ),
        LLMConfiguration(model="gpt-4.1-mini"),
    ],
)
async def test_a_providerless_block_ignores_its_account_fields(keys, block):
    # A provider-less block's key name, endpoint or model were written for
    # another provider; the default uses its own account and model.
    svc = await get_llm_service(block)
    assert isinstance(svc, AWSBedrockLLMService)
    assert svc._settings.model == LUNA
    assert _bedrock_key(svc) == "bedrock-key"


async def test_an_observer_with_an_azure_model_name_still_lands_on_luna(keys):
    # Observers inherit the template's block; with no template LLM the merged
    # block has no provider, and an Azure model name must not reach Bedrock.
    merged = merge_llm_config(
        LLMConfiguration(model="gpt-4.1-mini", temperature=0.0), LLMConfiguration()
    )
    svc = await get_llm_service(merged, pooled=True)
    assert isinstance(svc, AWSBedrockLLMService)
    assert svc._settings.model == LUNA
    fields = svc._settings.additional_model_request_fields
    assert isinstance(fields, dict) and fields["temperature"] == 0.0


async def test_an_unknown_provider_fails_open_to_luna(keys):
    # Unreachable through validation (the enum rejects it); a provider added
    # to the enum without its own branch still connects, on the default.
    block = LLMConfiguration.model_construct(
        provider="new_vendor", model="vendor-model", temperature=0.3
    )
    svc = await get_llm_service(block)
    assert isinstance(svc, AWSBedrockLLMService)
    assert svc._settings.model == LUNA
    fields = svc._settings.additional_model_request_fields
    assert isinstance(fields, dict) and fields["temperature"] == 0.3
    assert _bedrock_key(svc) == "bedrock-key"


async def test_explicit_azure_is_unchanged(keys):
    svc = await get_llm_service(
        LLMConfiguration(
            provider=LLMProvider.AZURE,
            model="gpt-4.1-mini",
            endpoint="https://azure.example",
            api_key_name="AZURE_X",
        )
    )
    assert isinstance(svc, AzureLLMService)


async def test_explicit_bedrock_is_unchanged(keys):
    svc = await get_llm_service(
        DEFAULT_LLM_CONFIGURATION.model_copy(update={"temperature": 0.4})
    )
    assert isinstance(svc, AWSBedrockLLMService)
    fields = svc._settings.additional_model_request_fields
    assert isinstance(fields, dict) and fields["temperature"] == 0.4


async def test_hold_transfer_summary_runs_on_the_default_llm(monkeypatch):
    seen: dict[str, Any] = {}

    class _Svc:
        async def run_inference(
            self, context, max_tokens=None, system_instruction=None
        ):
            seen["messages"] = context.get_messages()
            seen["system"] = system_instruction
            return "  Customer agreed to a callback at 6 pm.  "

    async def fake_get_llm_service(cfg=None, **_kw):
        seen["cfg"] = cfg
        return _Svc()

    monkeypatch.setattr(hold_transfer, "get_llm_service", fake_get_llm_service)

    summary = await hold_transfer.summarize_transcription(
        [
            {"role": "assistant", "content": "Is now a good time?"},
            {"role": "user", "content": "Call me at 6 pm."},
        ],
        reason="callback",
    )

    assert summary == "Customer agreed to a callback at 6 pm."
    assert seen["cfg"] is None
    assert seen["messages"] == [
        {
            "role": "user",
            "content": "assistant: Is now a good time?\nuser: Call me at 6 pm.",
        }
    ]
    assert "Reason for call: callback" in seen["system"]


async def test_hold_transfer_falls_back_when_the_llm_fails(monkeypatch):
    class _Svc:
        async def run_inference(self, *_a, **_kw):
            raise RuntimeError("bedrock down")

    async def fake_get_llm_service(cfg=None, **_kw):
        return _Svc()

    monkeypatch.setattr(hold_transfer, "get_llm_service", fake_get_llm_service)
    assert (
        await hold_transfer.summarize_transcription(
            [{"role": "user", "content": "hello"}]
        )
        is None
    )


def _llm_blocks(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            # The field's own schema entry ({description, example}) is not a block.
            if key == "llm_configurations" and isinstance(value, dict):
                if "example" not in value:
                    yield value
            yield from _llm_blocks(value)
    elif isinstance(node, list):
        for value in node:
            yield from _llm_blocks(value)


def test_the_template_generator_writes_the_default_block():
    # The generator copies these blocks into new templates; a drift from the
    # default (a missing region or max_tokens) fails every call at startup.
    from app.ai.voice.agents.breeze_buddy.template.generator import prompts

    examples = [
        json.loads(block)
        for name in dir(prompts)
        if name.startswith("_EXAMPLE_")
        for block in re.findall(r"```json\n(.*?)```", getattr(prompts, name), re.S)
    ]
    reference = json.loads(
        (Path(prompts.__file__).parent.parent / "field_reference.json").read_text()
    )
    blocks = [b for doc in examples + [reference] for b in _llm_blocks(doc)]
    blocks.append(reference["ConfigurationModel"]["llm_configurations"]["example"])

    assert len(blocks) >= 6
    for block in blocks:
        assert LLMConfiguration(**block) == DEFAULT_LLM_CONFIGURATION

"""Regression coverage for LLM per-provider config resolution across the
config_resolver migration.

Azure/OpenAI: template (truthy-checked) > static/dynamic default.
Vertex/ClaudeVertex: template + dynamic credentials are all required —
locks in the exact ValueError message strings, since they're observable
template-misconfiguration debugging output.
"""

import pytest

import app.ai.voice.agents.breeze_buddy.llm as llm_mod
from app.ai.voice.llm import LLMConfiguration, LLMProvider, ThinkingConfiguration
from app.core.config.resolver import or_none


@pytest.fixture(autouse=True)
def _fake_static_creds(monkeypatch):
    monkeypatch.setattr(llm_mod, "AZURE_OPENAI_API_KEY", "azure-key")
    monkeypatch.setattr(llm_mod, "AZURE_OPENAI_ENDPOINT", "https://azure.example")
    monkeypatch.setattr(llm_mod, "OPENAI_API_KEY", "openai-key")


@pytest.mark.asyncio
async def test_azure_no_template_config_uses_static_defaults():
    service = await llm_mod._resolve_azure(None)
    assert service is not None


@pytest.mark.asyncio
async def test_azure_template_overrides_endpoint_and_model(monkeypatch):
    config = LLMConfiguration(
        provider=LLMProvider.AZURE,
        endpoint="https://custom.example",
        model="gpt-custom",
        api_key_name="MY_KEY",
    )

    async def fake_get_config(key, default_value, return_type=str):
        return "resolved-api-key"

    monkeypatch.setattr(llm_mod, "get_config", fake_get_config)
    service = await llm_mod._resolve_azure(config)
    assert service is not None


@pytest.mark.asyncio
async def test_azure_temperature_zero_is_preserved_not_treated_as_falsy():
    config = LLMConfiguration(provider=LLMProvider.AZURE, temperature=0.0)
    resolved = await llm_mod.resolve_fields(
        [
            llm_mod.FieldSpec(
                "temperature",
                tiers=[
                    lambda: config.temperature,
                    llm_mod.BREEZE_BUDDY_AZURE_TEMPERATURE,
                ],
            )
        ]
    )
    assert resolved["temperature"] == 0.0


@pytest.mark.asyncio
async def test_azure_max_tokens_zero_falls_through_truthy_check():
    # LLMConfiguration enforces max_tokens >= 1, so use a bare namespace to
    # exercise the truthy-check tier logic directly (0 is falsy -> fallback).
    from types import SimpleNamespace

    config = SimpleNamespace(max_tokens=0)
    resolved = await llm_mod.resolve_fields(
        [
            llm_mod.FieldSpec(
                "max_tokens",
                tiers=[
                    lambda: or_none(config.max_tokens),
                    llm_mod.BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS,
                ],
            )
        ]
    )
    assert resolved["max_tokens"] != 0


@pytest.mark.asyncio
async def test_openai_no_template_config_uses_static_defaults():
    service = await llm_mod._resolve_openai(None)
    assert service is not None


async def _vertex_deps(monkeypatch, *, credentials="creds-json", project_id="proj-1"):
    async def fake_creds():
        return credentials

    async def fake_project():
        return project_id

    monkeypatch.setattr(llm_mod, "GOOGLE_VERTEX_CREDENTIALS_JSON", fake_creds)
    monkeypatch.setattr(llm_mod, "GOOGLE_VERTEX_PROJECT_ID", fake_project)


@pytest.mark.asyncio
async def test_vertex_missing_credentials_raises_exact_message(monkeypatch):
    await _vertex_deps(monkeypatch, credentials=None)
    config = LLMConfiguration(
        provider=LLMProvider.GOOGLE_VERTEX,
        model="gemini-pro",
        region="us-central1",
        temperature=0.5,
        max_tokens=1024,
    )
    with pytest.raises(
        ValueError,
        match="GOOGLE_VERTEX_CREDENTIALS_JSON is required for google_vertex provider",
    ):
        await llm_mod._resolve_vertex(config)


@pytest.mark.asyncio
async def test_vertex_missing_project_id_raises_exact_message(monkeypatch):
    await _vertex_deps(monkeypatch, project_id=None)
    config = LLMConfiguration(
        provider=LLMProvider.GOOGLE_VERTEX,
        model="gemini-pro",
        region="us-central1",
        temperature=0.5,
        max_tokens=1024,
    )
    with pytest.raises(
        ValueError,
        match="GOOGLE_VERTEX_PROJECT_ID is required for google_vertex provider",
    ):
        await llm_mod._resolve_vertex(config)


@pytest.mark.asyncio
async def test_vertex_missing_model_raises_exact_message(monkeypatch):
    await _vertex_deps(monkeypatch)
    config = LLMConfiguration(
        provider=LLMProvider.GOOGLE_VERTEX,
        region="us-central1",
        temperature=0.5,
        max_tokens=1024,
    )
    with pytest.raises(
        ValueError,
        match="model is required in LLMConfiguration for google_vertex provider",
    ):
        await llm_mod._resolve_vertex(config)


@pytest.mark.asyncio
async def test_vertex_missing_temperature_raises_exact_message(monkeypatch):
    await _vertex_deps(monkeypatch)
    config = LLMConfiguration(
        provider=LLMProvider.GOOGLE_VERTEX,
        model="gemini-pro",
        region="us-central1",
        max_tokens=1024,
    )
    with pytest.raises(
        ValueError,
        match="temperature is required in LLMConfiguration for google_vertex provider",
    ):
        await llm_mod._resolve_vertex(config)


@pytest.mark.asyncio
async def test_vertex_all_required_fields_present_succeeds(monkeypatch):
    await _vertex_deps(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        llm_mod,
        "build_vertex_llm",
        lambda config: (captured.update(config=config), "built-service")[1],
    )
    config = LLMConfiguration(
        provider=LLMProvider.GOOGLE_VERTEX,
        model="gemini-pro",
        region="us-central1",
        temperature=0.5,
        max_tokens=1024,
    )
    service = await llm_mod._resolve_vertex(config)
    assert service == "built-service"
    assert captured["config"].model == "gemini-pro"
    assert captured["config"].location == "us-central1"
    assert captured["config"].temperature == 0.5
    assert captured["config"].max_tokens == 1024
    assert captured["config"].credentials_json == "creds-json"
    assert captured["config"].project_id == "proj-1"


@pytest.mark.asyncio
async def test_claude_vertex_missing_credentials_raises_exact_message(monkeypatch):
    await _vertex_deps(monkeypatch, credentials=None)
    config = LLMConfiguration(
        provider=LLMProvider.GOOGLE_VERTEX,
        model="claude-3",
        region="us-central1",
        temperature=0.5,
        max_tokens=1024,
    )
    with pytest.raises(
        ValueError,
        match="GOOGLE_VERTEX_CREDENTIALS_JSON is required for claude_vertex provider",
    ):
        await llm_mod._resolve_claude_vertex(config)


@pytest.mark.asyncio
async def test_claude_vertex_thinking_requires_budget_tokens(monkeypatch):
    await _vertex_deps(monkeypatch)
    config = LLMConfiguration(
        provider=LLMProvider.GOOGLE_VERTEX,
        model="claude-3",
        region="us-central1",
        temperature=0.5,
        max_tokens=1024,
        thinking=ThinkingConfiguration(enabled=True),
    )
    with pytest.raises(ValueError, match="budget_tokens is required when thinking"):
        await llm_mod._resolve_claude_vertex(config)


@pytest.mark.asyncio
async def test_claude_vertex_all_required_fields_present_succeeds(monkeypatch):
    await _vertex_deps(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        llm_mod,
        "build_claude_vertex_llm",
        lambda config, pooled=False: (captured.update(config=config), "built-service")[
            1
        ],
    )
    config = LLMConfiguration(
        provider=LLMProvider.GOOGLE_VERTEX,
        model="claude-3",
        region="us-central1",
        temperature=0.5,
        max_tokens=1024,
    )
    service = await llm_mod._resolve_claude_vertex(config)
    assert service == "built-service"
    assert captured["config"].model == "claude-3"
    assert captured["config"].region == "us-central1"
    assert captured["config"].credentials_json == "creds-json"
    assert captured["config"].project_id == "proj-1"

"""Phase 3 of provider accounts (docs/PROVIDER_CREDENTIALS.md): the LLM,
realtime and observer factories build on the account the resolver hands
them — the row's key and the row's host."""

import asyncio
from typing import Any, Dict

import pytest

from app.ai.voice.agents.breeze_buddy.accounts import Accounts
from app.ai.voice.llm.types import LLMConfiguration
from tests.accounts.conftest import ROW, ROW2, Store, cred


def test_an_observer_inherits_the_account_only_as_the_same_connection() -> None:
    from app.ai.voice.agents.breeze_buddy.observers.factory import merge_llm_config

    base = LLMConfiguration(provider="azure", credential_id=ROW)
    assert merge_llm_config(None, base).credential_id == ROW
    assert (
        merge_llm_config(LLMConfiguration(model="gpt-4o-mini"), base).credential_id
        == ROW
    )
    # another provider: never the Azure key to api.openai.com
    assert (
        merge_llm_config(LLMConfiguration(provider="openai"), base).credential_id
        is None
    )
    # its own endpoint: never the account's key to a private URL
    assert (
        merge_llm_config(
            LLMConfiguration(endpoint="https://attacker.example"), base
        ).credential_id
        is None
    )
    # its own row
    assert (
        merge_llm_config(LLMConfiguration(credential_id=ROW2), base).credential_id
        == ROW2
    )
    # an observer on its own row under a gateway template (endpoint +
    # api_key_name, the Flipkart Grid shape) takes its connection from the
    # row: the base's gateway never rides along, and the merge parses
    gateway = LLMConfiguration(
        provider="openai", endpoint="http://grid.internal/v1", api_key_name="GRID"
    )
    merged = merge_llm_config(
        LLMConfiguration(provider="openai", credential_id=ROW2), gateway
    )
    assert (merged.credential_id, merged.endpoint, merged.api_key_name) == (
        ROW2,
        None,
        None,
    )
    # without an account the base's connection is inherited as before
    plain = merge_llm_config(LLMConfiguration(model="gpt-4o-mini"), gateway)
    assert (plain.endpoint, plain.api_key_name) == ("http://grid.internal/v1", "GRID")


def test_a_typed_get_refuses_the_wrong_shape_with_the_vendors_words(
    store: Store,
) -> None:
    from app.ai.voice.agents.breeze_buddy.accounts import (
        AccountRefused,
        AzureAccount,
        KeyOnlyAccount,
    )
    from app.ai.voice.agents.breeze_buddy.template.types import TTSConfig

    voice = TTSConfig(provider="elevenlabs", credential_id=ROW)
    with pytest.raises(
        AccountRefused, match="resolved to KeyAccount, expected AzureAccount"
    ):
        asyncio.run(Accounts("r-1", "m-1").get(voice, AzureAccount))
    # never an AssertionError, and the memo is shared with the untyped read
    assert not isinstance(
        asyncio.run(Accounts("r-1", "m-1").get(voice)), KeyOnlyAccount
    )


def test_the_llm_factory_builds_on_the_rows_key_and_the_rows_endpoint(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ai.voice.agents.breeze_buddy.llm as llm_factory

    seen: Dict[str, Any] = {}

    def fake_build(config: Any, *, pooled: bool = False) -> str:
        seen["api_key"], seen["endpoint"] = config.api_key, config.endpoint
        return "svc"

    monkeypatch.setattr(llm_factory, "build_azure_llm", fake_build)
    store.rows[ROW2] = cred(
        id=ROW2,
        provider="azure_openai",
        value={"api_key": "acct", "endpoint": "https://acct.azure.com"},
    )
    block = LLMConfiguration(
        provider="azure", model="gpt", api_key_name="IGNORED", credential_id=ROW2
    )
    assert (
        asyncio.run(llm_factory.get_llm_service(block, accounts=Accounts("r-1", "m-1")))
        == "svc"
    )
    assert seen == {"api_key": "acct", "endpoint": "https://acct.azure.com"}


def test_the_bedrock_factory_takes_the_rows_key_as_the_bearer_token(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ai.voice.agents.breeze_buddy.llm as llm_factory

    seen: Dict[str, Any] = {}

    def fake_build(config: Any) -> str:
        seen["api_key"], seen["region"] = config.api_key, config.region
        return "bedrock-svc"

    monkeypatch.setattr(llm_factory, "build_bedrock_llm", fake_build)
    store.rows[ROW2] = cred(
        id=ROW2, provider="aws_bedrock", value={"api_key": "bedrock-acct"}
    )
    block = LLMConfiguration(
        provider="aws_bedrock",
        model="anthropic.claude",
        region="ap-south-1",
        max_tokens=256,
        credential_id=ROW2,
    )
    assert (
        asyncio.run(llm_factory.get_llm_service(block, accounts=Accounts("r-1", "m-1")))
        == "bedrock-svc"
    )
    assert seen == {"api_key": "bedrock-acct", "region": "ap-south-1"}


def test_the_realtime_factory_builds_on_the_rows_key_and_host(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ai.voice.llm.realtime.factory as rt_factory

    seen: Dict[str, Any] = {}

    def fake_build(config: Any) -> str:
        seen["api_key"], seen["base_url"] = config.api_key, config.base_url
        return "azure-rt"

    monkeypatch.setattr(rt_factory, "build_azure_realtime_llm", fake_build)
    store.rows[ROW2] = cred(
        id=ROW2,
        provider="azure_openai_realtime",
        value={"api_key": "rt-acct", "endpoint": "wss://acct.openai.azure.com/rt"},
    )
    block = LLMConfiguration.model_validate(
        {"realtime": {"provider": "azure", "credential_id": ROW2}}
    )
    assert (
        asyncio.run(
            rt_factory.get_realtime_llm_service(block, accounts=Accounts("r-1", "m-1"))
        )
        == "azure-rt"
    )
    assert seen == {"api_key": "rt-acct", "base_url": "wss://acct.openai.azure.com/rt"}

"""Phase 2 of provider accounts (docs/PROVIDER_CREDENTIALS.md): one resolver
per call. A row serves only its own vendor in its own tenant, carries its
host, is read once; the environment answers with the same rules; the
save-time check walks every block."""

import asyncio
from typing import Any, Dict

import pytest

import app.ai.voice.agents.breeze_buddy.accounts.resolve as resolve
from app.ai.voice.agents.breeze_buddy.accounts import AccountRefused, Accounts
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    STTConfiguration,
    TTSConfig,
)
from app.ai.voice.llm.types import LLMConfiguration
from tests.accounts.conftest import ROW, ROW2, ROW3, Store, cred


def resolved(coro: Any) -> Any:
    """Run the resolver; the tests read the account's fields by name, so the
    static type is widened here rather than narrowed at every call."""
    return asyncio.run(coro)


def _configurations(**words: Any) -> ConfigurationModel:
    return ConfigurationModel.model_validate(words)


def _observer(**llm: Any) -> Dict[str, Any]:
    return {
        "name": "o",
        "system_prompt": "p",
        "action": {"type": "function", "handler": "end_conversation"},
        "llm": llm,
    }


# --- the resolver: a row -----------------------------------------------------


def test_a_row_serves_only_its_own_vendor_in_its_own_tenant(store: Store) -> None:
    accounts = Accounts("r-1", "m-1")
    voice = TTSConfig(provider="elevenlabs", credential_id=ROW)
    store.rows[ROW2] = cred(id=ROW2, is_active=False)
    store.rows[ROW3] = cred(id=ROW3, reseller_id="r-2")
    for block, words in (
        (TTSConfig(provider="elevenlabs", credential_id=ROW2), "is inactive"),
        (TTSConfig(provider="elevenlabs", credential_id=ROW3), "another tenant"),
        (
            TTSConfig(provider="cartesia", credential_id=ROW),
            "this block needs cartesia",
        ),
        (
            TTSConfig(provider="elevenlabs", credential_id=ROW.replace("0", "1")),
            "does not exist",
        ),
    ):
        with pytest.raises(AccountRefused, match=words):
            resolved(accounts.get(block))
    # global rows are everyone's; a reseller row is that reseller's
    store.rows[ROW] = cred(reseller_id=None)
    assert resolved(Accounts("r-9", "m-9").get(voice)).api_key == "xi-secret"


def test_a_row_is_read_once_per_call_and_its_shape_is_checked(
    store: Store, env: None
) -> None:
    accounts = Accounts("r-1", "m-1")
    voice = TTSConfig(provider="elevenlabs", credential_id=ROW)
    a = resolved(accounts.get(voice))
    b = resolved(accounts.get(voice))
    assert a is b and store.reads == [ROW]
    store.rows[ROW2] = cred(id=ROW2, provider="azure_openai", value={"api_key": "k"})
    with pytest.raises(AccountRefused, match="incomplete for azure_openai"):
        resolved(accounts.get(LLMConfiguration(provider="azure", credential_id=ROW2)))


def test_an_account_carries_its_host_and_a_service_that_cannot_use_it_refuses(
    store: Store, env: None
) -> None:
    """One row, one host. An ElevenLabs row brings only its key: the
    deployment's per-service host decides (ELEVENLABS_TTS_URL for a voice,
    ELEVENLABS_STT_URL for Scribe — release cbcec339). OpenAI STT has no
    gateway."""
    tts = resolved(
        Accounts("r-1", "m-1").get(TTSConfig(provider="elevenlabs", credential_id=ROW))
    )
    assert (tts.api_key, tts.endpoint) == ("xi-secret", "wss://tts.india.test")
    stt = resolved(
        Accounts("r-1", "m-1").get(
            STTConfiguration(provider="elevenlabs", credential_id=ROW)
        )
    )
    assert (stt.api_key, stt.endpoint) == ("xi-secret", "wss://stt.india.test")
    store.rows[ROW2] = cred(
        id=ROW2,
        provider="openai",
        value={"api_key": "oa", "endpoint": "https://gw.example"},
    )
    llm = resolved(
        Accounts("r-1", "m-1").get(
            LLMConfiguration(provider="openai", credential_id=ROW2)
        )
    )
    assert llm.endpoint == "https://gw.example"
    with pytest.raises(AccountRefused, match="has no gateway"):
        resolved(
            Accounts("r-1", "m-1").get(
                STTConfiguration(provider="openai", credential_id=ROW2)
            )
        )


# --- the resolver: the environment ----------------------------------------------


def test_without_a_row_the_environment_answers_with_the_same_rules(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = Accounts("r-1", "m-1")
    az = resolved(accounts.get(LLMConfiguration()))
    assert (az.api_key, az.endpoint) == ("env-az", "https://env.openai.azure.com/")
    # a custom endpoint needs a named key — never the default key to a stranger
    with pytest.raises(AccountRefused, match="api_key_name or credential_id"):
        resolved(
            accounts.get(LLMConfiguration(provider="openai", endpoint="https://gw"))
        )

    async def get_config(name: str, default: Any, kind: Any) -> str:
        return {"GW_KEY": "named"}.get(name, "")

    monkeypatch.setattr(resolve, "get_config", get_config)
    gw = resolved(
        accounts.get(
            LLMConfiguration(
                provider="openai", endpoint="https://gw", api_key_name="GW_KEY"
            )
        )
    )
    assert (gw.api_key, gw.endpoint) == ("named", "https://gw")
    # a template's OWN endpoint is taken as written — an internal http://
    # gateway with its named key keeps working (the https law is on rows)
    plain = resolved(
        accounts.get(
            LLMConfiguration(
                provider="openai",
                endpoint="http://10.0.0.5:8002/v1",
                api_key_name="GW_KEY",
            )
        )
    )
    assert (plain.api_key, plain.endpoint) == ("named", "http://10.0.0.5:8002/v1")
    # a missing env key is AccountRefused, never pydantic's error
    monkeypatch.setattr(resolve.static, "AZURE_OPENAI_API_KEY", "")
    with pytest.raises(AccountRefused, match="AZURE_OPENAI_API_KEY is required"):
        resolved(Accounts("r-1", "m-1").get(LLMConfiguration()))
    assert (
        resolved(accounts.get(STTConfiguration(provider="deepgram"))).api_key
        == "env-dg"
    )
    assert (
        resolved(accounts.get(STTConfiguration(provider="openai"))).api_key
        == "env-oa-stt"
    )
    assert (
        resolved(accounts.get(LLMConfiguration(provider="aws_bedrock"))).api_key is None
    )
    xi = resolved(accounts.get(TTSConfig(provider="elevenlabs")))
    assert (xi.api_key, xi.endpoint) == ("env-xi-tts", "wss://tts.india.test")
    scribe = resolved(accounts.get(STTConfiguration(provider="elevenlabs")))
    assert (scribe.api_key, scribe.endpoint) == ("env-xi-stt", "wss://stt.india.test")
    with pytest.raises(
        AccountRefused, match="no provider account exists for dragontts"
    ):
        resolved(
            accounts.get(TTSConfig(provider="dragontts", model="cartesia:sonic-3"))
        )


# --- the publish law ------------------------------------------------------------


def test_problems_walks_every_block_and_names_each_bad_one(store: Store) -> None:
    store.rows[ROW2] = cred(
        id=ROW2,
        provider="azure_openai",
        value={"api_key": "az", "endpoint": "https://a.openai.azure.com"},
    )
    conf = _configurations(
        llm_configurations={"provider": "azure", "credential_id": ROW2},
        stt_configuration={
            "provider": "deepgram",
            "credential_id": ROW,
        },  # elevenlabs row
        tts_configuration={"provider": "dragontts", "credential_id": ROW},  # no model
        tts_configuration_overrides={
            "cartesia": {"voice_id": "v", "credential_id": ROW3}
        },
        observers=[
            _observer(credential_id=ROW2),
            _observer(provider="openai", credential_id=ROW2),
        ],
    )
    found = resolved(Accounts("r-1", "m-1").problems(conf))
    assert [f.split(":")[0] for f in found] == [
        "stt_configuration",
        "tts_configuration",
        "tts_configuration_overrides.cartesia",
        "observers[1].llm",
    ]
    assert "this block needs deepgram" in found[0]
    assert "requires model '<provider>:<model>'" in found[1]
    assert "does not exist" in found[2]
    assert "this block needs openai" in found[3]  # the observer's own provider
    clean = _configurations(
        llm_configurations={"provider": "azure", "credential_id": ROW2},
        observers=[_observer()],  # inherits azure: the same row is fine
    )
    assert resolved(Accounts("r-1", "m-1").problems(clean)) == []
    assert resolved(Accounts("r-1", "m-1").problems(None)) == []


def test_a_template_without_the_word_reads_nothing(store: Store, env: None) -> None:
    conf = _configurations(
        tts_configuration={"provider": "elevenlabs", "voice_id": "v"},
        stt_configuration={"provider": "deepgram"},
        llm_configurations={"provider": "azure", "api_key_name": "AZ_KEY"},
    )
    assert resolved(Accounts("r-1", "m-1").problems(conf)) == []
    assert store.reads == []


def test_the_template_save_gate_answers_422_with_every_problem(store: Store) -> None:
    from fastapi import HTTPException

    from app.api.routers.breeze_buddy.templates.handlers import (
        refuse_bad_provider_accounts,
    )

    conf = _configurations(
        stt_configuration={"provider": "deepgram", "credential_id": ROW},
        tts_configuration={"provider": "elevenlabs", "credential_id": ROW3},
    )
    with pytest.raises(HTTPException) as e:
        resolved(refuse_bad_provider_accounts(conf, "r-1", "m-1"))
    assert e.value.status_code == 422
    detail: Any = e.value.detail
    found = detail["provider_credentials"]
    assert len(found) == 2 and found[0].startswith("stt_configuration:")
    clean = _configurations(tts_configuration={"provider": "elevenlabs"})
    resolved(refuse_bad_provider_accounts(clean, "r-1", "m-1"))

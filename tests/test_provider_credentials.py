"""Provider accounts a template runs on (docs/PROVIDER_CREDENTIALS.md).

One resolver per call (``Accounts``), typed accounts that carry their host,
the reference on the block. Pinned with a fake credential store and fake
environment — no database, no provider SDK.
"""

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

import app.ai.voice.agents.breeze_buddy.provider_credentials as pc
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    STTConfiguration,
    TTSConfig,
)
from app.ai.voice.llm.types import LLMConfiguration, RealtimeConfig
from app.database.queries.breeze_buddy.credentials import (
    delete_credential_query,
    get_credential_by_id_query,
    get_credentials_by_merchant_query,
    insert_credential_query,
    update_credential_query,
)
from app.schemas import Credential, CredentialType

ROW = "0ec1c06d-b2e2-4b38-8c19-f9789b3482bf"
ROW2 = "7b5028e7-41ce-46de-b120-6a819142f592"
ROW3 = "dad2e2ef-b228-4553-9cbe-bf24b4de9c9d"


def _cred(**over: Any) -> Credential:
    base: Dict[str, Any] = dict(
        id=ROW,
        reseller_id="r-1",
        merchant_id=None,
        name="elevenlabs-prod",
        credential_type=CredentialType.CUSTOM,
        value={"api_key": "xi-secret"},
        is_encrypted=True,
        is_active=True,
        provider="elevenlabs",
    )
    base.update(over)
    return Credential(**base)


class _Store:
    """The credential accessor, faked: id -> Credential."""

    def __init__(self, rows: List[Credential]) -> None:
        self.rows = {row.id: row for row in rows}
        self.reads: List[str] = []

    async def get_credential_by_id(
        self,
        credential_id: str,
        mask: bool = True,
        raise_errors: bool = False,
        placeholder_only: bool = False,
    ) -> Optional[Credential]:
        self.reads.append(credential_id)
        return self.rows.get(credential_id)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    s = _Store([_cred()])
    monkeypatch.setattr(pc, "get_credential_by_id", s.get_credential_by_id)
    return s


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake environment: every static key set, the ElevenLabs flag off."""
    for name, value in {
        "AZURE_OPENAI_API_KEY": "env-az",
        "AZURE_OPENAI_ENDPOINT": "https://env.openai.azure.com/",
        "OPENAI_API_KEY": "env-oa",
        "OPENAI_STT_API_KEY": "env-oa-stt",
        "DEEPGRAM_API_KEY": "env-dg",
        "SONIOX_API_KEY": "env-sx",
        "SARVAM_API_KEY": "env-sv",
        "ASSEMBLYAI_API_KEY": "env-aai",
        "CARTESIA_API_KEY": "env-ca",
        "ELEVENLABS_API_KEY": "env-xi",
        "ELEVENLABS_INDIAN_RESIDENCY_API_KEY": "env-xi-in",
        "ELEVENLABS_INDIAN_RESIDENCY_WEBSOCKET_URL": "wss://india.test",
        "GOOGLE_CREDENTIALS_JSON": "{}",
        "GEMINI_API_KEY": "env-gem",
    }.items():
        monkeypatch.setattr(pc.static, name, value)

    async def off() -> bool:
        return False

    monkeypatch.setattr(pc, "BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY", off)


def _configurations(**words: Any) -> ConfigurationModel:
    return ConfigurationModel.model_validate(words)


def _observer(**llm: Any) -> Dict[str, Any]:
    return {
        "name": "o",
        "system_prompt": "p",
        "action": {"type": "function", "handler": "end_conversation"},
        "llm": llm,
    }


# --- the words -----------------------------------------------------------------


def test_each_block_names_the_vendor_its_provider_word_means() -> None:
    assert pc.vendor_of(LLMConfiguration()) == "azure_openai"
    assert pc.vendor_of(LLMConfiguration(provider="google_vertex")) == "google_vertex"
    assert pc.vendor_of(LLMConfiguration(provider="aws_bedrock")) == "aws_bedrock"
    assert pc.vendor_of(RealtimeConfig(provider="azure")) == "azure_openai_realtime"
    assert pc.vendor_of(STTConfiguration(provider="deepgram")) == "deepgram"
    assert pc.vendor_of(TTSConfig(provider="gemini")) == "google"
    assert pc.vendor_of(TTSConfig(provider="dragontts")) == "tts:dragontts"
    assert pc.kind_of(RealtimeConfig(provider="openai")) == "realtime"


def test_a_value_must_have_its_vendors_shape() -> None:
    assert pc.shape_problems("azure_openai", {"api_key": "k"}) == [
        "endpoint: Field required"
    ]
    assert pc.shape_problems("azure_openai", {"api_key": "k", "endpoint": "x"}) == [
        "endpoint: Value error, endpoint 'x' is not a URL"
    ]
    assert pc.shape_problems("google_vertex", {"credentials_json": "{}"}) == [
        "project_id: Field required"
    ]
    assert pc.shape_problems("aws_bedrock", {}) == []  # the credential chain
    assert pc.shape_problems("deepgram", {"api_key": " "}) == [
        "api_key: Value error, api_key is empty"
    ]
    assert pc.shape_problems("nope", {"api_key": "k"})[0].startswith("unknown provider")


def test_a_dragontts_voice_with_an_account_is_its_nested_providers_voice() -> None:
    voice = TTSConfig(
        provider="dragontts", model="elevenlabs:eleven_flash_v2_5", credential_id=ROW
    )
    nested = pc.unwrap_dragontts(voice)
    assert (nested.provider.value, nested.model) == ("elevenlabs", "eleven_flash_v2_5")
    plain = TTSConfig(provider="dragontts", model="cartesia:sonic-3")
    assert pc.unwrap_dragontts(plain) is plain  # no account: the proxy path
    with pytest.raises(pc.AccountRefused):
        pc.unwrap_dragontts(TTSConfig(provider="dragontts", credential_id=ROW))


def test_the_blocks_carry_the_reference_in_one_spelling_and_never_an_endpoint():
    """Canonical credential_id at the edge, so an exact SQL match never misses
    an uppercase or hyphen-less spelling; and the endpoint law on the block."""
    block = LLMConfiguration(provider="azure", credential_id=ROW.upper())
    assert block.credential_id == ROW
    assert (
        TTSConfig(provider="cartesia", credential_id=ROW.replace("-", "")).credential_id
        == ROW
    )
    with pytest.raises(ValueError):
        LLMConfiguration(provider="azure", credential_id="not-a-uuid")
    with pytest.raises(ValueError):
        LLMConfiguration(provider="azure", credential_id=ROW, endpoint="https://x")
    with pytest.raises(ValueError):
        RealtimeConfig(provider="azure", credential_id=ROW, endpoint="wss://x")
    with pytest.raises(ValueError):
        LLMConfiguration(provider="google_vertex", region="attacker.example#")
    assert LLMConfiguration.__doc__ and RealtimeConfig.__doc__


# --- the resolver: a row -----------------------------------------------------


def test_a_row_serves_only_its_own_vendor_in_its_own_tenant(store: _Store) -> None:
    accounts = pc.Accounts("r-1", "m-1")
    voice = TTSConfig(provider="elevenlabs", credential_id=ROW)

    async def flag() -> bool:
        return False

    store.rows[ROW2] = _cred(id=ROW2, is_active=False)
    store.rows[ROW3] = _cred(id=ROW3, reseller_id="r-2")
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
        with pytest.raises(pc.AccountRefused, match=words):
            asyncio.run(accounts.get(block))
    # global rows are everyone's; a reseller row is that reseller's
    store.rows[ROW] = _cred(reseller_id=None)
    assert asyncio.run(pc.Accounts("r-9", "m-9").get(voice)).api_key == "xi-secret"


def test_a_row_is_read_once_per_call_and_its_shape_is_checked(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def flag() -> bool:
        return False

    monkeypatch.setattr(pc, "BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY", flag)
    accounts = pc.Accounts("r-1", "m-1")
    voice = TTSConfig(provider="elevenlabs", credential_id=ROW)
    a = asyncio.run(accounts.get(voice))
    b = asyncio.run(accounts.get(voice))
    assert a is b and store.reads == [ROW]
    store.rows[ROW2] = _cred(id=ROW2, provider="azure_openai", value={"api_key": "k"})
    with pytest.raises(pc.AccountRefused, match="incomplete for azure_openai"):
        asyncio.run(
            accounts.get(LLMConfiguration(provider="azure", credential_id=ROW2))
        )


def test_an_account_carries_its_host_and_a_service_that_cannot_use_it_refuses(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #8: one row, one host. ElevenLabs rows live on the deployment's
    cluster (the flag decides; the row is the key); OpenAI STT has no gateway."""
    india = {"on": True}

    async def flag() -> bool:
        return india["on"]

    monkeypatch.setattr(pc, "BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY", flag)
    monkeypatch.setattr(
        pc.static, "ELEVENLABS_INDIAN_RESIDENCY_WEBSOCKET_URL", "wss://india.test"
    )
    tts = asyncio.run(
        pc.Accounts("r-1", "m-1").get(
            TTSConfig(provider="elevenlabs", credential_id=ROW)
        )
    )
    assert (tts.api_key, tts.endpoint) == ("xi-secret", "wss://india.test")
    with pytest.raises(pc.AccountRefused, match="India-resident cluster"):
        asyncio.run(
            pc.Accounts("r-1", "m-1").get(
                STTConfiguration(provider="elevenlabs", credential_id=ROW)
            )
        )
    india["on"] = False
    stt = asyncio.run(
        pc.Accounts("r-1", "m-1").get(
            STTConfiguration(provider="elevenlabs", credential_id=ROW)
        )
    )
    assert (stt.api_key, stt.endpoint) == ("xi-secret", None)
    store.rows[ROW2] = _cred(
        id=ROW2,
        provider="openai",
        value={"api_key": "oa", "endpoint": "https://gw.example"},
    )
    llm = asyncio.run(
        pc.Accounts("r-1", "m-1").get(
            LLMConfiguration(provider="openai", credential_id=ROW2)
        )
    )
    assert llm.endpoint == "https://gw.example"
    with pytest.raises(pc.AccountRefused, match="has no gateway"):
        asyncio.run(
            pc.Accounts("r-1", "m-1").get(
                STTConfiguration(provider="openai", credential_id=ROW2)
            )
        )


# --- the resolver: the environment ----------------------------------------------


def test_without_a_row_the_environment_answers_with_the_same_rules(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = pc.Accounts("r-1", "m-1")
    az = asyncio.run(accounts.get(LLMConfiguration()))
    assert (az.api_key, az.endpoint) == ("env-az", "https://env.openai.azure.com/")
    # a custom endpoint needs a named key — never the default key to a stranger
    with pytest.raises(pc.AccountRefused, match="api_key_name or credential_id"):
        asyncio.run(
            accounts.get(LLMConfiguration(provider="openai", endpoint="https://gw"))
        )

    async def get_config(name: str, default: Any, kind: Any) -> str:
        return {"GW_KEY": "named"}.get(name, "")

    monkeypatch.setattr(pc, "get_config", get_config)
    gw = asyncio.run(
        accounts.get(
            LLMConfiguration(
                provider="openai", endpoint="https://gw", api_key_name="GW_KEY"
            )
        )
    )
    assert (gw.api_key, gw.endpoint) == ("named", "https://gw")
    assert (
        asyncio.run(accounts.get(STTConfiguration(provider="deepgram"))).api_key
        == "env-dg"
    )
    assert (
        asyncio.run(accounts.get(STTConfiguration(provider="openai"))).api_key
        == "env-oa-stt"
    )
    assert (
        asyncio.run(accounts.get(LLMConfiguration(provider="aws_bedrock"))).api_key
        is None
    )
    xi = asyncio.run(accounts.get(TTSConfig(provider="elevenlabs")))
    assert (xi.api_key, xi.endpoint) == ("env-xi", pc.ELEVENLABS_PUBLIC_HOST)
    with pytest.raises(
        pc.AccountRefused, match="no provider account exists for dragontts"
    ):
        asyncio.run(
            accounts.get(TTSConfig(provider="dragontts", model="cartesia:sonic-3"))
        )


# --- the publish law ------------------------------------------------------------


def test_problems_walks_every_block_and_names_each_bad_one(store: _Store) -> None:
    store.rows[ROW2] = _cred(
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
    found = asyncio.run(pc.Accounts("r-1", "m-1").problems(conf))
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
    assert asyncio.run(pc.Accounts("r-1", "m-1").problems(clean)) == []
    assert asyncio.run(pc.Accounts("r-1", "m-1").problems(None)) == []


def test_a_template_without_the_word_reads_nothing(store: _Store, env: None) -> None:
    conf = _configurations(
        tts_configuration={"provider": "elevenlabs", "voice_id": "v"},
        stt_configuration={"provider": "deepgram"},
        llm_configurations={"provider": "azure", "api_key_name": "AZ_KEY"},
    )
    assert asyncio.run(pc.Accounts("r-1", "m-1").problems(conf)) == []
    assert store.reads == []


# --- observers -------------------------------------------------------------------


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


# --- the factories build on the account ------------------------------------------


def test_the_llm_factory_builds_on_the_rows_key_and_the_rows_endpoint(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ai.voice.agents.breeze_buddy.llm as llm_factory

    seen: Dict[str, Any] = {}

    def fake_build(config: Any, *, pooled: bool = False) -> str:
        seen["api_key"], seen["endpoint"] = config.api_key, config.endpoint
        return "svc"

    monkeypatch.setattr(llm_factory, "build_azure_llm", fake_build)
    store.rows[ROW2] = _cred(
        id=ROW2,
        provider="azure_openai",
        value={"api_key": "acct", "endpoint": "https://acct.azure.com"},
    )
    block = LLMConfiguration(
        provider="azure", model="gpt", api_key_name="IGNORED", credential_id=ROW2
    )
    assert (
        asyncio.run(
            llm_factory.get_llm_service(block, accounts=pc.Accounts("r-1", "m-1"))
        )
        == "svc"
    )
    assert seen == {"api_key": "acct", "endpoint": "https://acct.azure.com"}


def test_the_bedrock_factory_takes_the_rows_key_as_the_bearer_token(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ai.voice.agents.breeze_buddy.llm as llm_factory

    seen: Dict[str, Any] = {}

    def fake_build(config: Any) -> str:
        seen["api_key"], seen["region"] = config.api_key, config.region
        return "bedrock-svc"

    monkeypatch.setattr(llm_factory, "build_bedrock_llm", fake_build)
    store.rows[ROW2] = _cred(
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
        asyncio.run(
            llm_factory.get_llm_service(block, accounts=pc.Accounts("r-1", "m-1"))
        )
        == "bedrock-svc"
    )
    assert seen == {"api_key": "bedrock-acct", "region": "ap-south-1"}


def test_the_stt_factory_builds_on_the_account(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ai.voice.agents.breeze_buddy.stt as stt_factory

    seen: Dict[str, Any] = {}

    def fake_build(config: Any) -> str:
        seen["api_key"] = config.api_key
        return "stt-svc"

    monkeypatch.setattr(stt_factory, "build_deepgram_stt", fake_build)
    store.rows[ROW2] = _cred(id=ROW2, provider="deepgram", value={"api_key": "dg-acct"})
    config = STTConfiguration(provider="deepgram", credential_id=ROW2)
    assert (
        asyncio.run(
            stt_factory.create_stt_from_config(
                config, accounts=pc.Accounts("r-1", "m-1")
            )
        )
        == "stt-svc"
    )
    assert seen == {"api_key": "dg-acct"}


def test_the_tts_factory_bypasses_dragontts_for_a_voice_with_an_account(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ai.voice.agents.breeze_buddy.tts as tts_factory

    seen: Dict[str, Any] = {}

    def fake_cartesia(config: Any) -> str:
        seen["api_key"], seen["model"] = config.api_key, config.model
        return "cartesia-svc"

    def fake_dragon(config: Any) -> str:
        raise AssertionError("DragonTTS must not be used for a voice with an account")

    async def off(*a: Any, **k: Any) -> Any:
        return False

    monkeypatch.setattr(tts_factory, "build_cartesia_tts", fake_cartesia)
    monkeypatch.setattr(tts_factory, "build_dragontts_tts", fake_dragon)
    monkeypatch.setattr(tts_factory, "BB_STRIP_EMOJIS_FROM_TTS", off)
    monkeypatch.setattr(tts_factory, "BB_AGGREGATE_SENTENCES", off)
    store.rows[ROW2] = _cred(id=ROW2, provider="cartesia", value={"api_key": "ca-acct"})
    voice = TTSConfig(
        provider="dragontts", model="cartesia:sonic-3", voice_id="v", credential_id=ROW2
    )
    svc = asyncio.run(
        tts_factory.get_tts_service(voice, accounts=pc.Accounts("r-1", "m-1"))
    )
    assert svc == "cartesia-svc" and seen == {"api_key": "ca-acct", "model": "sonic-3"}
    # the same voice naming an ElevenLabs row is refused: the row must be the
    # provider that really synthesizes (review, 24 Sep 2026)
    wrong = TTSConfig(
        provider="dragontts", model="cartesia:sonic-3", voice_id="v", credential_id=ROW
    )
    with pytest.raises(pc.AccountRefused, match="this block needs cartesia"):
        asyncio.run(
            tts_factory.get_tts_service(wrong, accounts=pc.Accounts("r-1", "m-1"))
        )


def test_the_elevenlabs_host_is_the_deployments_flag_and_the_row_only_brings_the_key(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ai.voice.agents.breeze_buddy.tts as tts_factory

    seen: Dict[str, Any] = {}

    def fake_elevenlabs(config: Any) -> str:
        seen["api_key"], seen["url"] = config.api_key, config.url
        return "elevenlabs-svc"

    async def off(*a: Any, **k: Any) -> Any:
        return False

    async def india(*a: Any, **k: Any) -> bool:
        return True

    monkeypatch.setattr(tts_factory, "build_elevenlabs_tts", fake_elevenlabs)
    monkeypatch.setattr(tts_factory, "BB_STRIP_EMOJIS_FROM_TTS", off)
    monkeypatch.setattr(tts_factory, "BB_AGGREGATE_SENTENCES", off)
    monkeypatch.setattr(
        pc.static, "ELEVENLABS_INDIAN_RESIDENCY_WEBSOCKET_URL", "wss://india.test"
    )
    monkeypatch.setattr(pc, "BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY", india)
    voice = TTSConfig(provider="elevenlabs", voice_id="v", credential_id=ROW)
    asyncio.run(tts_factory.get_tts_service(voice, accounts=pc.Accounts("r-1", "m-1")))
    assert seen == {"api_key": "xi-secret", "url": "wss://india.test"}
    monkeypatch.setattr(pc, "BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY", off)
    asyncio.run(tts_factory.get_tts_service(voice, accounts=pc.Accounts("r-1", "m-1")))
    assert seen == {"api_key": "xi-secret", "url": pc.ELEVENLABS_PUBLIC_HOST}


# --- the store: the guard is the statement -----------------------------------------


def test_the_provider_column_rides_insert_and_update() -> None:
    sql, params = insert_credential_query(
        id=ROW,
        reseller_id="r",
        merchant_id="m",
        name="n",
        credential_type="custom",
        value="v",
        is_encrypted=True,
        description=None,
        provider="deepgram",
    )
    assert '"provider"' in sql and params[-1] == "deepgram"
    sql, params = update_credential_query("c-1", provider="elevenlabs")
    assert '"provider" = $1' in sql and params[0] == "elevenlabs"


def test_the_in_use_guard_is_exact_atomic_and_inside_the_write() -> None:
    sql, params = delete_credential_query(ROW)
    assert "jsonb_path_exists" in sql and "'$.** ? (@.credential_id == $id)'" in sql
    assert "AND NOT" in sql and "LIKE" not in sql and params == [ROW]
    sql, params = update_credential_query(
        ROW, is_active=False, unless_named_by_a_template=True
    )
    assert "jsonb_path_exists" in sql and sql.count("$3") == 2 and params[-1] == ROW
    sql, _ = update_credential_query(ROW, description="d")
    assert "jsonb_path_exists" not in sql


def test_provider_rows_are_never_template_variables_nor_pre_check_context() -> None:
    for args in (("r", "m"), ("r", None), (None, None)):
        sql, _ = get_credentials_by_merchant_query(*args)
        assert '"provider" IS NULL' in sql
    sql, _ = get_credential_by_id_query(ROW, placeholder_only=True)
    assert '"provider" IS NULL' in sql
    sql, _ = get_credential_by_id_query(ROW)
    assert "provider" not in sql


# --- the credential API: writes enforce their own rules -----------------------------


def test_the_credential_api_judges_provider_rows_at_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    import app.api.routers.breeze_buddy.credentials.handlers as handlers
    from app.database.accessor.breeze_buddy.credentials import CredentialInUseError
    from app.schemas.breeze_buddy.credentials import (
        CreateCredentialRequest,
        UpdateCredentialRequest,
    )

    user = type("U", (), {"username": "t"})()

    def create(**over: Any) -> CreateCredentialRequest:
        base = dict(
            name="n",
            credential_type="custom",
            value={"api_key": "k"},
            provider="deepgram",
        )
        base.update(over)
        return CreateCredentialRequest(**base)

    # unknown provider -> 400; a known one without its fields -> 422
    with pytest.raises(HTTPException) as e:
        asyncio.run(
            handlers.create_credential_handler(create(provider="elevenlab"), user)
        )
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        asyncio.run(
            handlers.create_credential_handler(create(provider="azure_openai"), user)
        )
    assert e.value.status_code == 422

    existing = _cred(
        id=ROW,
        provider="azure_openai",
        value={"api_key": "real", "endpoint": "https://a.openai.azure.com"},
    )

    async def get_credential_by_id(*a: Any, **k: Any) -> Credential:
        return existing

    written: Dict[str, Any] = {}

    async def update_credential(**k: Any) -> Credential:
        if k.get("unless_named_by_a_template"):
            raise CredentialInUseError(ROW)
        written.update(k)
        return existing

    monkeypatch.setattr(handlers, "get_credential_by_id", get_credential_by_id)
    monkeypatch.setattr(handlers, "update_credential", update_credential)
    # a masked key must not move the host (review #1)
    with pytest.raises(HTTPException) as e:
        asyncio.run(
            handlers.update_credential_handler(
                ROW,
                UpdateCredentialRequest(
                    value={"api_key": "******", "endpoint": "https://attacker.example"}
                ),
                user,
            )
        )
    assert e.value.status_code == 422 and "requires its full key" in str(e.value.detail)
    # the full key may move the host
    asyncio.run(
        handlers.update_credential_handler(
            ROW,
            UpdateCredentialRequest(
                credential_type="custom",
                value={"api_key": "new", "endpoint": "https://b.openai.azure.com"},
            ),
            user,
        )
    )
    assert written["value"]["endpoint"] == "https://b.openai.azure.com"
    # deactivating or re-labelling a row templates name: the statement refuses -> 409
    for req in (
        UpdateCredentialRequest(is_active=False),
        UpdateCredentialRequest(provider="deepgram"),
    ):
        with pytest.raises(HTTPException) as e:
            asyncio.run(handlers.update_credential_handler(ROW, req, user))
        assert e.value.status_code == 409

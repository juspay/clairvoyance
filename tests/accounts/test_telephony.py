"""Phase 5 of provider accounts (docs/PROVIDER_CREDENTIALS.md): a template's
``telephony_configuration`` names the Plivo account its calls run on, the
way ``stt_configuration`` names its STT account. The same rules: a row
serves only as a Plivo account, only in its tenant, only when complete, a
bad reference is a 422 at save, and a named row that may not serve is never
swapped for the environment's keys."""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from app.ai.voice.agents.breeze_buddy.accounts import (
    AccountRefused,
    Accounts,
    PlivoAccount,
    shape_problems,
    template_plivo_account,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.plivo import (
    PlivoProvider,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    TelephonyConfiguration,
)
from app.core.config import static
from app.schemas import Credential
from tests.accounts.conftest import ROW, Store, cred

US_ID = "MAUS0000000000000001"
IN_ID = "MAIN0000000000000001"
US_KEYS = {"auth_id": US_ID, "auth_token": "us-token"}


def plivo_cred(**over: Any) -> Credential:
    base: Dict[str, Any] = dict(name="plivo-us", provider="plivo", value=dict(US_KEYS))
    base.update(over)
    return cred(**base)


def configurations(credential_id: Any = ROW) -> ConfigurationModel:
    return ConfigurationModel.model_validate(
        {
            "telephony_configuration": {
                "provider": "plivo",
                "credential_id": credential_id,
            }
        }
    )


@pytest.fixture
def plivo_row(store: Store) -> Store:
    store.rows[ROW] = plivo_cred()
    return store


@pytest.fixture
def env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment's (India) Plivo keys."""
    monkeypatch.setattr(static, "PLIVO_AUTH_ID", IN_ID)
    monkeypatch.setattr(static, "PLIVO_AUTH_TOKEN", "in-token")


@pytest.fixture
def env_provider(env_keys: None) -> PlivoProvider:
    """A Plivo provider on the environment's keys."""
    return PlivoProvider(aiohttp_session=None)


# --- the shape and the block ------------------------------------------------


def test_a_plivo_row_is_exactly_an_auth_id_and_token() -> None:
    assert shape_problems("plivo", US_KEYS) == []
    assert len(shape_problems("plivo", {})) == 2
    assert shape_problems("plivo", {**US_KEYS, "auth_token": " "}) == [
        "auth_token: Value error, auth_token is empty"
    ]
    assert shape_problems("plivo", {**US_KEYS, "endpoint": "https://x"})
    # the SDK's own rule, met at the write rather than when a call builds it
    assert shape_problems("plivo", {**US_KEYS, "auth_id": "MAUS"})


def test_the_block_stores_its_credential_id_in_one_spelling() -> None:
    assert TelephonyConfiguration(credential_id=ROW.upper()).credential_id == ROW
    with pytest.raises(ValueError, match="is not a UUID"):
        TelephonyConfiguration(credential_id="not-a-uuid")
    with pytest.raises(ValueError):
        TelephonyConfiguration.model_validate({"provider": "twilio"})


# --- the save gate: the same check STT and TTS blocks get -------------------


@pytest.mark.parametrize(
    "row, words",
    [
        (plivo_cred(is_active=False), "is inactive"),
        (plivo_cred(provider="elevenlabs"), "needs plivo"),
        (plivo_cred(reseller_id="r-2"), "another tenant"),
        (plivo_cred(value={"auth_id": US_ID}), "incomplete for plivo"),
    ],
)
def test_a_bad_reference_is_named_at_save(
    store: Store, row: Credential, words: str
) -> None:
    store.rows[ROW] = row
    [problem] = asyncio.run(Accounts("r-1", "m-1").problems(configurations()))
    assert problem.startswith("telephony_configuration: ") and words in problem


def test_a_good_reference_saves_clean(plivo_row: Store) -> None:
    assert asyncio.run(Accounts("r-1", "m-1").problems(configurations())) == []


# --- the resolver -----------------------------------------------------------


def test_a_template_naming_no_row_runs_on_the_environment(
    store: Store, env_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = Accounts("r-1", "m-1")
    for configs in (ConfigurationModel(), configurations(None)):
        account = asyncio.run(template_plivo_account(accounts, configs))
        assert (account.auth_id, account.auth_token) == (IN_ID, "in-token")
    assert store.reads == []
    # the environment's keys obey the same shape, and a missing one refuses
    monkeypatch.setattr(static, "PLIVO_AUTH_ID", "not-an-id")
    with pytest.raises(AccountRefused, match="not a Plivo auth id") as refused:
        asyncio.run(template_plivo_account(Accounts("r-1"), ConfigurationModel()))
    # the field and the reason, never the value
    assert "not-an-id" not in str(refused.value)
    monkeypatch.setattr(static, "PLIVO_AUTH_ID", "")
    with pytest.raises(AccountRefused, match="PLIVO_AUTH_ID"):
        asyncio.run(template_plivo_account(Accounts("r-1"), ConfigurationModel()))


def test_a_template_runs_on_its_row_in_its_tenant(plivo_row: Store) -> None:
    account = asyncio.run(template_plivo_account(Accounts("r-1"), configurations()))
    assert isinstance(account, PlivoAccount) and account.auth_id == US_ID
    with pytest.raises(AccountRefused, match="another tenant"):
        asyncio.run(template_plivo_account(Accounts("r-2"), configurations()))


# --- the provider: the call's transfer and hang-up on one account -----------


def _serializer_on(provider: PlivoProvider) -> SimpleNamespace:
    serializer = SimpleNamespace(_auth_id=IN_ID, _auth_token="in-token")
    transport = SimpleNamespace(
        output=lambda: SimpleNamespace(_params=SimpleNamespace(serializer=serializer))
    )
    provider.set_hangup_credentials(transport)
    return serializer


def test_a_call_runs_on_its_templates_account(
    plivo_row: Store, env_provider: PlivoProvider
) -> None:
    provider = env_provider
    assert provider.client.session.auth == (IN_ID, "in-token")
    asyncio.run(provider.use_template_credentials(Accounts("r-1"), configurations()))
    assert provider.client.session.auth == (US_ID, "us-token")
    # the transfer runs on the same client, the hang-up on the same keys
    assert provider.conference_service.client is provider.client
    serializer = _serializer_on(provider)
    assert (serializer._auth_id, serializer._auth_token) == (US_ID, "us-token")


def test_a_refused_account_ends_the_call_like_stt_and_tts(
    store: Store, env_provider: PlivoProvider
) -> None:
    store.rows[ROW] = plivo_cred(is_active=False)
    with pytest.raises(AccountRefused, match="is inactive"):
        asyncio.run(
            env_provider.use_template_credentials(Accounts("r-1"), configurations())
        )
    # nothing was switched: no call runs on another org's keys
    assert env_provider.client.session.auth == (IN_ID, "in-token")


def test_a_template_with_no_account_keeps_the_environment(
    store: Store, env_provider: PlivoProvider
) -> None:
    asyncio.run(
        env_provider.use_template_credentials(Accounts("r-1"), ConfigurationModel())
    )
    assert env_provider.client.session.auth == (IN_ID, "in-token")
    assert _serializer_on(env_provider)._auth_id == IN_ID


def test_a_call_keeps_the_account_it_started_on(
    plivo_row: Store, env_provider: PlivoProvider
) -> None:
    """An agent-to-agent transfer runs a new generation on another template;
    the call leg still belongs to the account the call started on."""
    first = asyncio.run(
        env_provider.use_template_credentials(Accounts("r-1"), configurations())
    )
    assert first is True
    assert env_provider.client.session.auth == (US_ID, "us-token")
    again = asyncio.run(
        env_provider.use_template_credentials(Accounts("r-1"), ConfigurationModel())
    )
    assert again is False
    assert env_provider.client.session.auth == (US_ID, "us-token")

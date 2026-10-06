"""The Plivo REST paths outside the live call: they run on the account the
lead's template names, the MPC callback always answers the waiting bot, and
a recording's keys go only to Plivo."""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

import app.ai.voice.agents.breeze_buddy.services.telephony.plivo.account as plivo_account
import app.ai.voice.agents.breeze_buddy.services.telephony.plivo.plivo as plivo_mod
import app.ai.voice.agents.breeze_buddy.services.telephony.plivo.recording as recording
from app.ai.voice.agents.breeze_buddy.accounts import AccountRefused, PlivoAccount
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.account import (
    lead_plivo_account,
)
from app.ai.voice.agents.breeze_buddy.template.types import ConfigurationModel
from app.schemas import LeadCallTracker
from tests.accounts.conftest import Store
from tests.accounts.test_telephony import (  # noqa: F401 — fixtures
    IN_ID,
    US_ID,
    configurations,
    env_keys,
    plivo_row,
)


def test_after_the_call_the_lead_runs_on_its_templates_account(
    plivo_row: Store, env_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = {
        "t-account": SimpleNamespace(configurations=configurations()),
        "t-plain": SimpleNamespace(configurations=ConfigurationModel()),
    }

    async def get_template_by_id(template_id: str) -> Any:
        return templates.get(template_id)

    monkeypatch.setattr(plivo_account, "get_template_by_id_cached", get_template_by_id)

    def lead(template_id: Any) -> LeadCallTracker:
        return LeadCallTracker(
            id="lead-1", reseller_id="r-1", template="barclays", template_id=template_id
        )

    # the template names an account: the call ran on it
    assert asyncio.run(lead_plivo_account(lead("t-account"))).auth_id == US_ID
    # no account on the template, or no template: the environment's
    assert asyncio.run(lead_plivo_account(lead("t-plain"))).auth_id == IN_ID
    assert asyncio.run(lead_plivo_account(lead(None))).auth_id == IN_ID


@pytest.mark.parametrize("exc", [AccountRefused("inactive"), RuntimeError("db")])
def test_the_mpc_callback_always_answers_the_waiting_bot(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    published: List[Any] = []

    async def lead(_call_sid: str) -> Any:
        return None

    async def account(_lead: Any) -> Any:
        raise exc

    async def publish(channel: str, payload: Dict[str, Any]) -> None:
        published.append((channel, payload))

    monkeypatch.setattr(plivo_mod, "get_lead_by_call_id", lead)
    monkeypatch.setattr(plivo_mod, "lead_plivo_account", account)
    monkeypatch.setattr(plivo_mod, "publish_hold_transfer_result", publish)
    join = {
        "call_sid": "CA-1",
        "EventName": "ParticipantJoin",
        "MPCName": "transfer-CA-1",
        "ParticipantRole": "agent",
    }
    asyncio.run(plivo_mod.handle_mpc_transfer_webhook(join))
    assert published == [
        (
            "transfer_outcome:CA-1",
            {"status": "unavailable", "reason": "account_unresolved"},
        )
    ]


@pytest.fixture
def gets(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, Any]]:
    """aiohttp.ClientSession, faked: each GET's (url, auth), answered 200."""
    seen: List[Tuple[str, Any]] = []

    class Response:
        status = 200

        async def read(self) -> bytes:
            return b"mp3"

        async def __aenter__(self) -> "Response":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    class Session:
        def get(self, url: str, auth: Any = None, proxy: Any = None) -> Response:
            seen.append((url, auth))
            return Response()

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    monkeypatch.setattr(recording.aiohttp, "ClientSession", Session)
    return seen


@pytest.mark.parametrize(
    "url, keys_sent",
    [
        ("https://media.plivo.com/v1/Account/x/Recording/y.mp3", True),
        # a regional recording host (India's is aps1)
        ("https://aps1.media.plivo.com/v1/Account/x/Recording/y.mp3", True),
        # Plivo's API is not a recording: never signed
        ("https://api.plivo.com/v1/Account/x/Call/", False),
        # our uploaded copy on the storage CDN: fetched, but without keys
        ("https://sdk.beta.breezesdk.store/breeze-buddy/recordings/r.mp3", False),
        ("https://attacker.example/rec.mp3", False),
        ("http://media.plivo.com/v1/Account/x/Recording/y.mp3", False),
        ("https://plivo.com.attacker.example/rec.mp3", False),
    ],
)
def test_a_recordings_keys_go_only_to_plivo_over_https(
    gets: List[Tuple[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    keys_sent: bool,
) -> None:
    looked_up: List[Any] = []

    async def account(lead: Any) -> PlivoAccount:
        looked_up.append(lead)
        return PlivoAccount(auth_id=US_ID, auth_token="t")

    lead = LeadCallTracker(id="lead-1", reseller_id="r-1", template="barclays")

    async def get_lead_by_call_id(call_sid: str) -> Any:
        assert call_sid == "CA-1"
        return lead

    monkeypatch.setattr(recording, "get_lead_by_call_id", get_lead_by_call_id)
    monkeypatch.setattr(recording, "lead_plivo_account", account)
    audio = asyncio.run(recording.download_call_recording(url, "CA-1"))
    assert audio is not None and audio.read() == b"mp3"
    [(fetched, auth)] = gets
    assert fetched == url
    if keys_sent:
        assert auth is not None and auth.login == US_ID
    else:
        # any other URL is fetched without keys, and without reading the
        # lead's account at all
        assert auth is None and looked_up == []


@pytest.mark.parametrize(
    "found, exc",
    [(True, AccountRefused("inactive")), (True, RuntimeError("db")), (False, None)],
)
def test_a_recording_whose_account_is_unresolved_is_not_downloaded(
    gets: List[Tuple[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    found: bool,
    exc: Any,
) -> None:
    lead = LeadCallTracker(id="lead-1", reseller_id="r-1", template="barclays")

    async def get_lead_by_call_id(_call_sid: str) -> Any:
        return lead if found else None  # None: no lead, or a swallowed read

    async def account(_lead: Any) -> PlivoAccount:
        raise exc

    monkeypatch.setattr(recording, "get_lead_by_call_id", get_lead_by_call_id)
    monkeypatch.setattr(recording, "lead_plivo_account", account)
    url = "https://media.plivo.com/v1/Account/x/Recording/y.mp3"
    # None, as for any failed download: the callback keeps the Plivo URL,
    # GET recording answers 502 — never a fetch on other keys
    assert asyncio.run(recording.download_call_recording(url, "CA-1")) is None
    assert gets == []

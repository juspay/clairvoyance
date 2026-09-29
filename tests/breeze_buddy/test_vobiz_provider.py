"""
Tests for Vobiz as a telephony provider: the vocabulary, the outbound dial,
and the channel accounting.

A Vobiz number must be storable (enum + both provider CHECKs), must take and
return channels exactly like a Plivo number, and ``VobizProvider.make_call``
must send the request Vobiz documents and answer the dispatch worker in the
only three shapes it understands:

- ``{"status": "call_initiated", "sid": ...}`` — the call was placed;
- ``None`` — Vobiz did not place it (non-2xx or transport error), so the
  worker un-records the attempt against the customer's call limit;
- a dict WITHOUT ``"sid"`` — a 2xx we cannot read may still have rung, so the
  attempt stays counted.

Getting the second and third mixed up either re-dials a customer who was just
rung or silently drops a lead. The dial also carries the account's
X-Auth-Token, so it never follows a redirect and the base URL must be https
(http only for a localhost stand-in). Nothing here reaches Vobiz, Postgres or
Redis: ``requests.post`` and the channel accessors are patched where the code
looks them up.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

# dispatch must import first: managers.calls and dispatch.worker import each
# other, and only this order resolves it (the order the app itself loads them
# in). "...breeze_buddy" sorts before "...breeze_buddy.managers", so isort
# keeps this line above the next one on its own.
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.ai.voice.agents.breeze_buddy.services.telephony.utils import (
    get_voice_provider,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.vobiz import vobiz as vz
from app.schemas import CallProvider
from app.schemas.breeze_buddy.core import TelephonyNumber, TelephonyNumberStatus
from app.services.langfuse.tasks.score_monitor import score as score_mod

APP = "https://bb.example.com"
API_BASE = "https://vobiz.test/api/v1"
AUTH_ID = "MA_TEST0001"
AUTH_TOKEN = "vz-token-secret"
CUSTOMER = "919000000001"
CALLER_ID = "918000000901"
REQUEST_UUID = "5a9fd4a0-3d4c-11ef-bef9-0242ac110005"
ANSWER = f"{APP}/agent/voice/breeze-buddy/vobiz/answer"

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "app" / "database" / "migrations"


# ── helpers ──────────────────────────────────────────────────────────────


def make_response(status: int, body: bytes) -> requests.Response:
    """A real requests.Response, so .json() / .text behave exactly as they
    do against Vobiz (e.g. .json() on HTML raises requests' own
    JSONDecodeError, not the stdlib one)."""
    resp = requests.Response()
    resp.status_code = status
    resp._content = body
    resp.encoding = "utf-8"
    return resp


class PostSpy:
    """Stands in for requests.post: records every call, answers or raises."""

    def __init__(
        self,
        status: int = 200,
        body: bytes = b'{"api_id": "a-1", "message": "Call fired", '
        b'"request_uuid": "' + REQUEST_UUID.encode() + b'"}',
        raises: Optional[Exception] = None,
    ) -> None:
        self.status = status
        self.body = body
        self.raises = raises
        self.calls: List[Tuple[str, dict]] = []

    def __call__(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append((url, kwargs))
        if self.raises is not None:
            raise self.raises
        return make_response(self.status, self.body)


@pytest.fixture
def vobiz_api(monkeypatch):
    """Freeze the static config the provider imported by name, turn the egress
    proxy off, and hand back an installer for the fake ``requests.post``."""
    monkeypatch.setattr(vz, "APP_BASE_URL", APP)
    monkeypatch.setattr(vz, "VOBIZ_API_BASE_URL", API_BASE)
    monkeypatch.setattr(vz, "VOBIZ_AUTH_ID", AUTH_ID)
    monkeypatch.setattr(vz, "VOBIZ_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(vz, "get_proxy_config", lambda: None)

    def _install(**kwargs: Any) -> PostSpy:
        spy = PostSpy(**kwargs)
        monkeypatch.setattr(vz.requests, "post", spy)
        return spy

    return _install


def make_number(provider: Any = CallProvider.VOBIZ) -> TelephonyNumber:
    return TelephonyNumber(
        id="num-1",
        number=CALLER_ID,
        provider=provider,
        status=TelephonyNumberStatus.AVAILABLE,
        channels=0,
        maximum_channels=4,
    )


class ChannelSpy:
    """Records which channel accessor _acquire/_release_number reached."""

    def __init__(self, row: Any = True) -> None:
        self.row = row
        self.calls: List[Tuple[str, Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "ChannelSpy":
        async def increment(number_id: str):
            self.calls.append(("increment", number_id))
            return self.row

        async def decrement(number_id: str):
            self.calls.append(("decrement", number_id))
            return self.row

        async def set_status(number_id: str, status: TelephonyNumberStatus):
            self.calls.append(("status", (number_id, status)))
            return self.row

        monkeypatch.setattr(calls_mod, "increment_telephony_number_channels", increment)
        monkeypatch.setattr(calls_mod, "decrement_telephony_number_channels", decrement)
        monkeypatch.setattr(calls_mod, "update_telephony_number_status", set_status)
        return self


# ── the vocabulary ───────────────────────────────────────────────────────


def test_vobiz_is_a_stored_provider_value():
    """The DB row, the API body and the template config all carry the plain
    string; it must parse back to the enum member."""
    assert CallProvider("VOBIZ") is CallProvider.VOBIZ
    assert CallProvider.VOBIZ.value == "VOBIZ"
    assert make_number(provider="VOBIZ").provider is CallProvider.VOBIZ


def test_the_provider_factory_builds_a_vobiz_provider_for_a_vobiz_number():
    """The dispatch worker dials through get_voice_provider(number.provider).
    Vobiz has no conference API wired, so transfers must see None
    ("unavailable") rather than a service that talks to Plivo."""
    provider = get_voice_provider(CallProvider.VOBIZ, None)
    assert isinstance(provider, vz.VobizProvider)
    assert provider.conference_service is None


# ── make_call: the request on the wire ───────────────────────────────────


async def test_make_call_sends_the_documented_request(vobiz_api):
    """Driven the way the dispatch worker drives it (factory, then the
    thread-offloaded make_call_async).

    Docs: https://www.vobiz.ai/docs/call/make-call — "POST
    https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/" (trailing slash
    required), JSON body, from/to/answer_url/answer_method/hangup_url/
    hangup_method; https://www.vobiz.ai/docs/api-reference/authentication —
    X-Auth-ID / X-Auth-Token headers, not HTTP Basic.
    """
    spy = vobiz_api()
    provider = get_voice_provider(CallProvider.VOBIZ, None)

    result = await provider.make_call_async(
        CUSTOMER, CALLER_ID, reseller_id="res-1", template_name="cod confirm & pay"
    )

    assert result == {"status": "call_initiated", "sid": REQUEST_UUID}
    assert len(spy.calls) == 1, "one dial must be exactly one POST"
    url, kwargs = spy.calls[0]
    assert url == f"{API_BASE}/Account/{AUTH_ID}/Call/"
    assert kwargs["headers"] == {"X-Auth-ID": AUTH_ID, "X-Auth-Token": AUTH_TOKEN}
    assert kwargs["timeout"] == 30, "a black-holed Vobiz must not hang the worker"
    assert kwargs["proxies"] is None
    assert kwargs["allow_redirects"] is False
    assert "data" not in kwargs, "Vobiz takes a JSON body, not a form"
    assert kwargs["json"] == {
        "from": CALLER_ID,
        "to": CUSTOMER,
        "answer_url": f"{ANSWER}?reseller_id=res-1&template=cod+confirm+%26+pay",
        "answer_method": "POST",
        "hangup_url": f"{APP}/agent/voice/breeze-buddy/vobiz/callback/status",
        "hangup_method": "POST",
    }
    # The quoting round-trips: the answer handler reads back what we sent,
    # and the "&" inside the template did not become a third parameter.
    query = parse_qs(urlsplit(kwargs["json"]["answer_url"]).query)
    assert query == {"reseller_id": ["res-1"], "template": ["cod confirm & pay"]}


@pytest.mark.parametrize(
    "reseller_id,template_name,expected",
    [
        (None, None, ANSWER),
        ("res-1", None, f"{ANSWER}?reseller_id=res-1"),
        # The worker passes `template_id or ""`: empty means "no tag".
        ("res-1", "", f"{ANSWER}?reseller_id=res-1"),
        (None, "welcome", f"{ANSWER}?template=welcome"),
        (
            "res 1/a",
            "t=1?x#y",
            f"{ANSWER}?reseller_id=res+1%2Fa&template=t%3D1%3Fx%23y",
        ),
    ],
)
def test_answer_url_carries_only_the_params_it_was_given_url_quoted(
    vobiz_api, reseller_id, template_name, expected
):
    spy = vobiz_api()
    vz.VobizProvider(None).make_call(
        CUSTOMER, CALLER_ID, reseller_id=reseller_id, template_name=template_name
    )
    assert spy.calls[0][1]["json"]["answer_url"] == expected


def test_make_call_goes_through_the_egress_proxy_when_one_is_configured(
    vobiz_api, monkeypatch
):
    spy = vobiz_api()
    monkeypatch.setattr(vz, "get_proxy_config", lambda: "http://proxy.internal:3128")
    vz.VobizProvider(None).make_call(CUSTOMER, CALLER_ID)
    assert spy.calls[0][1]["proxies"] == {
        "https": "http://proxy.internal:3128",
        "http": "http://proxy.internal:3128",
    }


# ── make_call: what the worker is told ───────────────────────────────────


@pytest.mark.parametrize("status", [200, 201])
def test_2xx_with_a_request_uuid_is_a_placed_call(vobiz_api, status):
    """Docs: https://www.vobiz.ai/docs/call/make-call — success is
    {"api_id", "message": "Call fired", "request_uuid"}; request_uuid is the
    call id every later callback is keyed on."""
    vobiz_api(status=status)
    result = vz.VobizProvider(None).make_call(CUSTOMER, CALLER_ID)
    assert result == {"status": "call_initiated", "sid": REQUEST_UUID}


@pytest.mark.parametrize("status", [400, 401, 402, 404, 429, 500])
def test_non_2xx_means_the_call_was_not_placed(vobiz_api, status):
    """None, even if the error body happens to carry a request_uuid: the
    status decides, and None is what makes the worker un-record the attempt."""
    vobiz_api(status=status, body=b'{"request_uuid": "should-not-be-used"}')
    assert vz.VobizProvider(None).make_call(CUSTOMER, CALLER_ID) is None


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_not_followed_and_means_the_call_was_not_placed(
    vobiz_api, status
):
    """requests keeps custom headers across a redirect (it strips only
    Authorization on a host change), so following one would hand X-Auth-ID /
    X-Auth-Token to whatever host — or plain-http URL — it names. Not
    followed, a 3xx placed nothing, even with a request_uuid in the body."""
    spy = vobiz_api(
        status=status, body=b'{"request_uuid": "' + REQUEST_UUID.encode() + b'"}'
    )
    assert vz.VobizProvider(None).make_call(CUSTOMER, CALLER_ID) is None
    assert spy.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ConnectionError("connection refused"),
        requests.exceptions.Timeout("read timed out"),
    ],
    ids=["connection-error", "timeout"],
)
def test_transport_error_means_the_call_was_not_placed(vobiz_api, error):
    """Raised before any reply from Vobiz: nothing rang, and the worker must
    get None back rather than an exception."""
    spy = vobiz_api(raises=error)
    assert vz.VobizProvider(None).make_call(CUSTOMER, CALLER_ID) is None
    assert len(spy.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"<html>ok</html>",
        b"",
        b"[]",
        b"null",
        b'"queued"',
        b"{}",
        b'{"request_uuid": 123}',
        b'{"request_uuid": ""}',
        b'{"request_uuid": null}',
    ],
    ids=[
        "non-json",
        "empty",
        "list",
        "null",
        "string",
        "no-uuid",
        "int-uuid",
        "empty-uuid",
        "null-uuid",
    ],
)
def test_2xx_without_a_usable_request_uuid_is_a_reply_without_a_sid(vobiz_api, body):
    """Vobiz accepted the request, so the call may have rung: the worker must
    get a dict WITHOUT "sid" (attempt stays counted), never None and never an
    exception."""
    vobiz_api(status=200, body=body)
    result = vz.VobizProvider(None).make_call(CUSTOMER, CALLER_ID)
    assert isinstance(result, dict)
    assert "sid" not in result
    assert result["status"] != "call_initiated"


# ── the base URL the token is sent to ────────────────────────────────────


def import_static_with(base_url: str) -> subprocess.CompletedProcess:
    """Boot the static config in a fresh interpreter: it is read once at
    import, and reloading the shared module here would leak into every other
    test."""
    env = {**os.environ, "PYTHONPATH": str(REPO), "VOBIZ_API_BASE_URL": base_url}
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.core.config.static as s; print(s.VOBIZ_API_BASE_URL)",
        ],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    "base_url,expected",
    [
        ("https://api.vobiz.ai/api/v1", "https://api.vobiz.ai/api/v1"),
        ("https://api.vobiz.ai/api/v1/", "https://api.vobiz.ai/api/v1"),
        ("http://localhost:8791/api/v1", "http://localhost:8791/api/v1"),
        ("http://127.0.0.1:8791/api/v1", "http://127.0.0.1:8791/api/v1"),
        ("http://[::1]:8791/api/v1", "http://[::1]:8791/api/v1"),
        # Set but empty (an empty chart secret) is "not configured": the
        # https default, never a boot failure on pods that never dial Vobiz.
        ("", "https://api.vobiz.ai/api/v1"),
    ],
)
def test_an_https_or_local_base_url_boots(base_url, expected):
    result = import_static_with(base_url)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == expected


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.vobiz.ai/api/v1",
        "http://10.0.0.5/api/v1",
        "http://localhost.evil.com/api/v1",
        "ftp://x",
        "https:///api/v1",
        "api.vobiz.ai/api/v1",
    ],
)
def test_a_base_url_that_would_leak_the_token_refuses_to_boot(base_url):
    """Every dial sends X-Auth-Token to this URL: plain http to a real host
    (or no usable host at all) must stop the pod at boot, not at first dial."""
    result = import_static_with(base_url)
    assert result.returncode != 0
    assert (
        "ValueError: VOBIZ_API_BASE_URL must use https (http only for localhost)"
        in result.stderr
    ), result.stderr


# ── channel accounting ───────────────────────────────────────────────────


@pytest.mark.parametrize("provider", [CallProvider.PLIVO, CallProvider.VOBIZ])
@pytest.mark.parametrize("row,acquired", [(True, True), (None, False)])
async def test_a_vobiz_number_takes_a_channel_exactly_like_plivo(
    monkeypatch, provider, row, acquired
):
    """One atomic channel increment; the accessor's None (at capacity or the
    UPDATE failed) means the number was NOT acquired."""
    spy = ChannelSpy(row=row).install(monkeypatch)
    assert await calls_mod._acquire_number(make_number(provider)) is acquired
    assert spy.calls == [("increment", "num-1")]


@pytest.mark.parametrize("provider", [CallProvider.PLIVO, CallProvider.VOBIZ])
async def test_a_vobiz_number_returns_exactly_one_channel_like_plivo(
    monkeypatch, provider
):
    spy = ChannelSpy().install(monkeypatch)
    await calls_mod._release_number("num-1", provider)
    assert spy.calls == [("decrement", "num-1")]


async def test_an_unknown_provider_is_never_acquired(monkeypatch):
    """Fail closed: a provider the accounting does not know takes nothing and
    is refused, so it can never be dialled past its ceiling."""
    spy = ChannelSpy().install(monkeypatch)
    stranger = TelephonyNumber.model_construct(id="num-1", provider="SIGNALWIRE")
    assert await calls_mod._acquire_number(stranger) is False
    assert spy.calls == []


# ── migration 081 ────────────────────────────────────────────────────────

PROVIDER_CHECKS = (
    "telephony_numbers_provider_check",
    "call_execution_config_calling_provider_check",
)
# 038 renamed the table's constraint; before it, the same CHECK was added
# under the old name.
RENAMED = {"outbound_number_provider_check": "telephony_numbers_provider_check"}
_ADD_IN_CHECK = re.compile(
    r"ADD\s+CONSTRAINT\s+(\w+)\s+CHECK\s*\(\s*\w+\s+IN\s*\(([^)]*)\)\s*\)", re.I
)


def provider_check_history() -> dict:
    """{constraint: [(migration file, allowed providers), ...]} in apply order."""
    history: dict = {name: [] for name in PROVIDER_CHECKS}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        for name, values in _ADD_IN_CHECK.findall(path.read_text()):
            name = RENAMED.get(name, name)
            if name in history:
                history[name].append((path.name, set(re.findall(r"'(\w+)'", values))))
    return history


@pytest.mark.parametrize("constraint", PROVIDER_CHECKS)
def test_migration_081_lets_both_provider_columns_store_vobiz(constraint):
    """Without it, creating a Vobiz number or pointing a template's
    calling_provider at VOBIZ is a CheckViolation the accessor swallows."""
    by_file = dict(provider_check_history()[constraint])
    assert "VOBIZ" in by_file["081_add_vobiz_provider.sql"]

    # ADD CONSTRAINT on a name that still exists fails the migration.
    text = (MIGRATIONS / "081_add_vobiz_provider.sql").read_text()
    drop = text.index(f"DROP CONSTRAINT IF EXISTS {constraint}")
    assert drop < text.index(f"ADD CONSTRAINT {constraint}")


@pytest.mark.parametrize("constraint", PROVIDER_CHECKS)
def test_no_provider_migration_ever_drops_a_provider(constraint):
    """Each provider migration re-states the whole list. Every restatement
    must be a superset of the one before, or the next person to add a
    provider silently makes every existing number of some other provider
    unwritable."""
    history = provider_check_history()[constraint]
    assert len(history) >= 2, f"parsed too few definitions of {constraint}"
    for (before_file, before), (after_file, after) in zip(history, history[1:]):
        dropped = before - after
        assert not dropped, f"{after_file} drops {sorted(dropped)} ({before_file})"


@pytest.mark.parametrize("constraint", PROVIDER_CHECKS)
def test_every_call_provider_is_allowed_by_the_latest_check(constraint):
    """The enum and the CHECK are one vocabulary: a provider the code can
    pick but the DB refuses fails only at runtime."""
    _file, latest = provider_check_history()[constraint][-1]
    assert {p.value for p in CallProvider} <= latest


# ── daily summary ────────────────────────────────────────────────────────


async def test_daily_summary_counts_vobiz_calls_in_the_provider_split(monkeypatch):
    """The split is fed from template calling_provider (any case) and only
    counts keys it was seeded with. The Slack text built from it is inside a
    large send method and is not pinned here."""
    row = SimpleNamespace(status=None, outcome=None)

    async def trackers(**kwargs):
        return [(row, "VOBIZ"), (row, "vobiz"), (row, "PLIVO")]

    async def no_leads(**kwargs):
        return []

    monkeypatch.setattr(score_mod, "get_all_lead_call_trackers", trackers)
    monkeypatch.setattr(score_mod, "get_lead_based_analytics", no_leads)

    stats = await score_mod.ScoreMonitor()._get_daily_call_stats()
    assert stats["provider_split"] == {
        "TWILIO": 0,
        "EXOTEL": 0,
        "PLIVO": 1,
        "VOBIZ": 2,
    }

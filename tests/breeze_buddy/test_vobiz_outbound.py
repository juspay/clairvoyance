"""
Tests for the outbound Vobiz answer webhook and hangup callback.

Vobiz speaks Plivo's XML dialect, so this change routes it through the Plivo
answer and status code. That makes two regressions cheap to ship and
expensive to notice: Plivo's own answer XML drifting while Vobiz is carved out
of it (every Plivo call in production reads that XML), and an unanswered Vobiz
call never being retried. What is proven here:

- ``/vobiz/answer`` is a supported answer webhook (an unknown provider is
  still refused), and an outbound call gets a ``<Stream>`` Vobiz can read:
  its three attributes, the websocket URL as the bare element text, and no
  Plivo-only noise-cancellation attributes, even with that flag on;
- the Plivo answer XML is byte-for-byte what release produced, with noise
  cancellation off and on;
- Vobiz's hangup callback reports CallStatus "completed" for every call, so a
  not-answered hangup code is read as no-answer / busy — the retry path keys
  on that status. Its docs name the key both ``CallStatus`` and ``Status``;
  either is read, ``CallStatus`` first;
- ``/vobiz/callback/status`` keys the call on CallUUID and feeds the same
  downstream as the Plivo branch (pod release, completed-call reconcile,
  retry), and the Plivo branch does not pick up the Vobiz correction.

Routes run through FastAPI's TestClient at the app's real prefix. Lead and
template lookups, Smart Router, and the retry / reconcile side effects are
patched, so nothing here touches Postgres, Redis or a provider.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

# dispatch must import first: managers.calls and dispatch.worker import each
# other, and only this order resolves it (the order the app itself loads them
# in). "...breeze_buddy" sorts before "...breeze_buddy.services", so isort
# keeps this line above the next one on its own.
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo import (
    recording as plivo_recording,
)
from app.api.routers.breeze_buddy.telephony import router as telephony_router
from app.api.routers.breeze_buddy.telephony.answer import handlers as ans_mod
from app.api.routers.breeze_buddy.telephony.callbacks import handlers as cb_mod

# app/main.py mounts the breeze_buddy router (which carries telephony) here;
# Vobiz's answer_url / hangup_url point at this prefix.
PREFIX = "/agent/voice/breeze-buddy"
BASE_URL = "https://bb.example.com"
TEMPLATE_ID = "tmpl-1"
FROM_NUMBER = "918000000901"
TO_NUMBER = "919000000001"

STREAM_ATTRIBUTES = {
    "bidirectional": "true",
    "keepCallAlive": "true",
    "contentType": "audio/x-mulaw;rate=8000",
}

# What release (f46d7449) answered a Plivo outbound call with, for the inputs
# pinned in ``outbound_call`` below. Hard-coded rather than recomputed: Plivo
# reads this XML on every call, so any byte of drift is a behaviour change.
# Note the double space after ``<Stream`` when noise cancellation is off.
_PLIVO_RECORD = (
    '    <Record recordSession="true" recordChannelType="mono" maxLength="3600" '
    'callbackUrl="https://bb.example.com/agent/voice/breeze-buddy/plivo/callback/'
    'details?call_uuid=plivo-call-1" callbackMethod="POST"/>\n'
)
_PLIVO_WS_URL = (
    "        wss://bb.example.com/agent/voice/breeze-buddy/plivo/callback/ws/v2"
    "?template_id=tmpl-1&amp;from_number=918000000901&amp;to_number=919000000001\n"
)
RELEASE_PLIVO_XML_NC_OFF = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<Response>\n"
    + _PLIVO_RECORD
    + '    <Stream  bidirectional="true" keepCallAlive="true" '
    'contentType="audio/x-mulaw;rate=8000">\n' + _PLIVO_WS_URL + "    </Stream>\n"
    "</Response>"
)
RELEASE_PLIVO_XML_NC_ON = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<Response>\n"
    + _PLIVO_RECORD
    + '    <Stream noiseCancellation="true" noiseCancellationLevel="high" '
    'bidirectional="true" keepCallAlive="true" '
    'contentType="audio/x-mulaw;rate=8000">\n' + _PLIVO_WS_URL + "    </Stream>\n"
    "</Response>"
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(telephony_router, prefix=PREFIX)
    return TestClient(app)


def outbound_call(monkeypatch: pytest.MonkeyPatch, noise_cancellation: bool) -> None:
    """Make every CallUUID resolve to an outbound lead on TEMPLATE_ID, with no
    Smart Router pod (so the shared websocket URL is used)."""

    async def get_lead(call_sid: str) -> SimpleNamespace:
        return SimpleNamespace(
            id="lead-1",
            template_id=TEMPLATE_ID,
            reseller_id="res-1",
            merchant_id="merchant-1",
        )

    async def get_template(template_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=template_id)

    async def no_pod(**_kwargs: Any) -> None:
        return None

    async def nc_enabled() -> bool:
        return noise_cancellation

    async def nc_level() -> str:
        return "high"

    monkeypatch.setattr(ans_mod, "get_lead_by_call_id", get_lead)
    monkeypatch.setattr(ans_mod, "get_template_by_id", get_template)
    monkeypatch.setattr(ans_mod, "safe_allocate_pod", no_pod)
    monkeypatch.setattr(ans_mod, "BB_NOISE_CANCELLATION_ENABLED", nc_enabled)
    monkeypatch.setattr(ans_mod, "BB_NOISE_CANCELLATION_LEVEL", nc_level)
    monkeypatch.setattr(ans_mod, "APP_BASE_URL", BASE_URL)
    monkeypatch.setattr(plivo_recording, "APP_BASE_URL", BASE_URL)
    monkeypatch.setattr(plivo_recording, "PLIVO_RECORDING_TIME_LIMIT", 3600)


def answer(client: TestClient, provider: str, call_id: str):
    return client.post(
        f"{PREFIX}/{provider}/answer",
        data={
            "CallUUID": call_id,
            "From": FROM_NUMBER,
            "To": TO_NUMBER,
            "Direction": "outbound",
        },
    )


# ── /vobiz/answer ────────────────────────────────────────────────────────


@pytest.mark.parametrize("provider", ["vobiz", "VOBIZ"])
def test_an_outbound_vobiz_answer_is_a_stream_vobiz_can_read(
    client, monkeypatch, provider
):
    """Vobiz takes the WebSocket URL as the ``<Stream>`` element's text, and
    documents no noise-cancellation attribute — so Plivo's (sent whenever the
    flag is on, as it is here) must not leak into a Vobiz answer, and the URL
    must not carry Plivo's surrounding whitespace.
    https://www.vobiz.ai/docs/xml/stream (bidirectional, keepCallAlive,
    contentType ``audio/x-mulaw;rate=8000``; URL as element text). The answer
    request is form-encoded ``CallUUID, From, To, Direction``:
    https://vobiz.ai/docs/xml/overview/how-it-works.

    The child count is deliberately not pinned: a ``<Record/>`` may precede
    the stream. The stream must be the last verb — keepCallAlive holds the
    call there.
    """
    outbound_call(monkeypatch, noise_cancellation=True)

    r = answer(client, provider, "vz-call-1")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert "noiseCancellation" not in r.text
    root = ET.fromstring(r.text)
    assert root.tag == "Response"
    streams = root.findall("Stream")
    assert len(streams) == 1
    stream = streams[0]
    assert root[-1] is stream, "Stream must be the final verb"
    assert stream.attrib == STREAM_ATTRIBUTES
    # Exact equality: the vobiz websocket path, every query param, the
    # XML-escaped '&' decoded back, and no whitespace around the URL.
    assert stream.text == (
        f"wss://bb.example.com{PREFIX}/vobiz/callback/ws/v2"
        f"?template_id={TEMPLATE_ID}&from_number={FROM_NUMBER}"
        f"&to_number={TO_NUMBER}"
    )


@pytest.mark.parametrize("provider", ["twilio", "bogus"])
def test_unknown_providers_are_still_refused_at_the_answer_webhook(
    client, monkeypatch, provider
):
    outbound_call(monkeypatch, noise_cancellation=False)

    r = answer(client, provider, "call-1")

    assert r.status_code == 404
    assert r.json() == {
        "detail": f"Provider '{provider}' is not supported for answer webhooks"
    }


@pytest.mark.parametrize(
    "noise_cancellation,expected",
    [(False, RELEASE_PLIVO_XML_NC_OFF), (True, RELEASE_PLIVO_XML_NC_ON)],
    ids=["noise-cancellation-off", "noise-cancellation-on"],
)
def test_the_plivo_answer_is_byte_for_byte_what_release_sent(
    client, monkeypatch, noise_cancellation, expected
):
    """Vobiz was carved out of the Plivo XML builder; Plivo must not notice."""
    outbound_call(monkeypatch, noise_cancellation=noise_cancellation)

    r = answer(client, "plivo", "plivo-call-1")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert r.text == expected


# ── the Vobiz hangup status ──────────────────────────────────────────────


@pytest.mark.parametrize("status_key", ["CallStatus", "Status"])
@pytest.mark.parametrize(
    "status,code,expected",
    [
        ("completed", "3000", "no-answer"),
        ("completed", "6010", "no-answer"),
        ("completed", "3010", "busy"),
        ("completed", "3020", "busy"),
        ("Completed", "3000", "no-answer"),
        # Answered calls, or no usable code: the status stands.
        ("completed", "4000", "completed"),
        ("completed", "4010", "completed"),
        ("completed", "", "completed"),
        ("completed", None, "completed"),
        # Anything but "completed" passes through untouched.
        ("no-answer", None, "no-answer"),
        ("busy", "4000", "busy"),
        ("failed", None, "failed"),
        ("timeout", "3000", "timeout"),
        # No status at all is "no status", whatever the code says.
        (None, "3000", None),
        (None, None, None),
    ],
)
def test_a_completed_vobiz_hangup_with_a_not_answered_code_is_that_failure(
    status_key: str,
    status: Optional[str],
    code: Optional[str],
    expected: Optional[str],
):
    """Vobiz's make-call page says the hangup_url CallStatus is 'Always
    "completed"' (https://www.vobiz.ai/docs/call/make-call), so without this
    correction no unanswered Vobiz call would ever be retried. Codes:
    https://www.vobiz.ai/docs/concepts/hangup-causes — 3000 No Answer,
    3010 Busy Line, 3020 Rejected, 6010 Ring Timeout Reached; 4000 Normal
    Hangup and 4010 End Of XML Instructions are answered calls.

    The docs disagree on the key: make-call lists ``CallStatus``, the hangup
    example on https://vobiz.ai/docs/concepts/callbacks sends ``Status``.
    Both must read the same.
    """
    fields: Dict[str, str] = {}
    if status is not None:
        fields[status_key] = status
    if code is not None:
        fields["HangupCauseCode"] = code
    assert cb_mod._vobiz_call_status(FormData(fields)) == expected


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"CallStatus": "busy", "Status": "completed"}, "busy"),
        (
            {"CallStatus": "completed", "Status": "busy", "HangupCauseCode": "3000"},
            "no-answer",
        ),
        ({"CallStatus": "completed", "Status": "no-answer"}, "completed"),
    ],
)
def test_call_status_wins_when_a_vobiz_callback_sends_both_keys(
    fields: Dict[str, str], expected: str
):
    assert cb_mod._vobiz_call_status(FormData(fields)) == expected


# ── /{provider}/callback/status ──────────────────────────────────────────


def record_downstream(monkeypatch: pytest.MonkeyPatch) -> List[tuple]:
    """Replace everything the status callback triggers with recorders, in the
    module where the handler looks each one up. Returns the event log."""
    events: List[tuple] = []

    async def release_pod(call_sid: str, reason: str) -> None:
        events.append(("release_pod", call_sid, reason))

    def reconcile(call_sid: str) -> tuple:
        # Stands in for the coroutine; spawn below records it.
        return ("reconcile", call_sid)

    def spawn(job: Any, name: Optional[str] = None) -> None:
        events.append(job)

    async def no_lead(call_sid: str) -> None:
        return None

    async def retry(call_sid: str) -> None:
        events.append(("retry", call_sid))

    monkeypatch.setattr(cb_mod, "safe_release_pod", release_pod)
    monkeypatch.setattr(cb_mod, "reconcile_completed_call", reconcile)
    monkeypatch.setattr(cb_mod, "spawn_background_task", spawn)
    monkeypatch.setattr(cb_mod, "get_lead_by_call_id", no_lead)
    monkeypatch.setattr(cb_mod, "handle_unanswered_calls", retry)
    return events


def retried(sid: str, status: str) -> List[tuple]:
    return [("release_pod", sid, f"status_{status}"), ("retry", sid)]


def reconciled(sid: str) -> List[tuple]:
    return [("release_pod", sid, "status_completed"), ("reconcile", sid)]


@pytest.mark.parametrize(
    "provider,fields,expected",
    [
        # Vobiz: the not-answered codes reach the retry path.
        (
            "vobiz",
            {"CallUUID": "vz-1", "CallStatus": "completed", "HangupCauseCode": "3000"},
            retried("vz-1", "no-answer"),
        ),
        (
            "vobiz",
            {"CallUUID": "vz-1", "CallStatus": "completed", "HangupCauseCode": "6010"},
            retried("vz-1", "no-answer"),
        ),
        (
            "vobiz",
            {"CallUUID": "vz-1", "CallStatus": "completed", "HangupCauseCode": "3010"},
            retried("vz-1", "busy"),
        ),
        (
            "vobiz",
            {"CallUUID": "vz-1", "CallStatus": "no-answer"},
            retried("vz-1", "no-answer"),
        ),
        # Vobiz: an answered call is reconciled, never retried.
        (
            "vobiz",
            {"CallUUID": "vz-1", "CallStatus": "completed", "HangupCauseCode": "4000"},
            reconciled("vz-1"),
        ),
        ("vobiz", {"CallUUID": "vz-1", "CallStatus": "completed"}, reconciled("vz-1")),
        # Vobiz: the "Status" spelling of the key drives the same paths.
        (
            "vobiz",
            {"CallUUID": "vz-1", "Status": "completed", "HangupCauseCode": "3000"},
            retried("vz-1", "no-answer"),
        ),
        ("vobiz", {"CallUUID": "vz-1", "Status": "completed"}, reconciled("vz-1")),
        # Vobiz: no status means nothing to act on.
        ("vobiz", {"CallUUID": "vz-1", "HangupCauseCode": "3000"}, []),
        # Plivo is unchanged: it sends real failure statuses, so a
        # "completed" is a completed call whatever code rides along.
        (
            "plivo",
            {"CallUUID": "pl-1", "CallStatus": "completed", "HangupCauseCode": "3000"},
            reconciled("pl-1"),
        ),
        (
            "plivo",
            {"CallUUID": "pl-1", "CallStatus": "no-answer"},
            retried("pl-1", "no-answer"),
        ),
        (
            "plivo",
            {"CallUUID": "pl-1", "CallStatus": "busy"},
            retried("pl-1", "busy"),
        ),
        # ...and it does not pick up the Vobiz "Status" fallback either.
        ("plivo", {"CallUUID": "pl-1", "Status": "no-answer"}, []),
    ],
)
def test_the_hangup_callback_feeds_the_shared_status_downstream(
    client, monkeypatch, provider, fields, expected
):
    """Pod release, the completed-call reconcile and the retry are the same
    functions for Plivo and Vobiz; only the status read differs."""
    events = record_downstream(monkeypatch)

    r = client.post(f"{PREFIX}/{provider}/callback/status", data=fields)

    assert r.status_code == 200
    assert events == expected


def test_a_vobiz_callback_keys_the_call_on_call_uuid_not_call_sid(client, monkeypatch):
    """Vobiz names the call CallUUID (https://www.vobiz.ai/docs/call/make-call,
    'Parameters Sent to hangup_url'); a stray CallSid must not be used."""
    events = record_downstream(monkeypatch)

    r = client.post(
        f"{PREFIX}/vobiz/callback/status",
        data={
            "CallSid": "decoy",
            "CallUUID": "vz-1",
            "CallStatus": "completed",
            "HangupCauseCode": "3000",
        },
    )

    assert r.status_code == 200
    assert events == retried("vz-1", "no-answer")

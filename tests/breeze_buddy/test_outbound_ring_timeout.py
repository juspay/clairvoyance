"""OUTBOUND_RING_TIMEOUT_SECONDS: how long a customer's phone rings on a Plivo
or Vobiz dial before the provider gives up and sends the hang-up callback.

Off (unset or 0) sends nothing, so each provider keeps its own default — the
request is exactly what it was before the setting existed. On, Plivo gets
``ring_timeout`` through its SDK, and Vobiz gets ``ring_timeout`` in the
make-call body (not ``hangup_on_ring``, which Vobiz documents as running from
the start of ringing to the hangup, answered or not).

The Plivo dial goes through the real SDK; only its HTTP send is replaced, so
these assert what would reach Plivo. Nothing here reaches Plivo or Vobiz.
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
import requests

import app.ai.voice.agents.breeze_buddy.services.telephony.plivo.plivo as plivo_mod
import app.ai.voice.agents.breeze_buddy.services.telephony.vobiz.vobiz as vz
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.plivo import (
    PlivoProvider,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.vobiz.vobiz import (
    VobizProvider,
)
from app.core.config import static

CUSTOMER = "+919000000001"
NUMBER = "+918000000001"


def plivo_dial(monkeypatch: pytest.MonkeyPatch, ring: int) -> Dict[str, Any]:
    """Dial through the real Plivo SDK; return the JSON body it would send."""
    monkeypatch.setattr(plivo_mod, "OUTBOUND_RING_TIMEOUT_SECONDS", ring)
    monkeypatch.setattr(static, "PLIVO_AUTH_ID", "MATEST00000000000001")
    monkeypatch.setattr(static, "PLIVO_AUTH_TOKEN", "test-token")
    provider = PlivoProvider(aiohttp_session=None)
    provider.APP_BASE_URL = "https://bb.example.test"  # the SDK checks the URL
    sent: List[Any] = []

    def send(prepared: requests.PreparedRequest, **_kwargs: Any) -> requests.Response:
        sent.append(prepared)
        response = requests.Response()
        response.status_code = 201
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(
            {"api_id": "a-1", "message": "call fired", "request_uuid": "u-1"}
        ).encode()
        response.url = prepared.url or ""
        return response

    monkeypatch.setattr(provider.client.session, "send", send)
    assert provider.make_call(CUSTOMER, NUMBER) == {
        "status": "call_initiated",
        "sid": "u-1",
    }
    [request] = sent
    assert request.url and request.url.endswith("/Call/")
    return json.loads(request.body or b"{}")


def vobiz_dial(monkeypatch: pytest.MonkeyPatch, ring: int) -> Dict[str, Any]:
    """Dial Vobiz; return the JSON body of the make-call request."""
    monkeypatch.setattr(vz, "OUTBOUND_RING_TIMEOUT_SECONDS", ring)
    sent: List[Dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> Any:
        sent.append(kwargs["json"])
        return SimpleNamespace(
            status_code=201, json=lambda: {"request_uuid": "u-1"}, text=""
        )

    monkeypatch.setattr(vz.requests, "post", post)
    assert VobizProvider(None).make_call(CUSTOMER, NUMBER) == {
        "status": "call_initiated",
        "sid": "u-1",
    }
    [payload] = sent
    return payload


# ── off: each provider's own default, the request unchanged ─────────────


@pytest.mark.parametrize("ring", [0, -5])
def test_off_plivo_rings_for_the_sdks_own_default(monkeypatch, ring):
    assert plivo_dial(monkeypatch, ring)["ring_timeout"] == 120


@pytest.mark.parametrize("ring", [0, -5])
def test_off_vobiz_is_sent_no_ring_timeout(monkeypatch, ring):
    payload = vobiz_dial(monkeypatch, ring)
    assert "ring_timeout" not in payload
    assert "hangup_on_ring" not in payload
    assert set(payload) == {
        "from",
        "to",
        "answer_url",
        "answer_method",
        "hangup_url",
        "hangup_method",
    }


# ── on: the customer's phone rings for that long, then the provider hangs up


def test_on_plivo_rings_for_the_configured_seconds(monkeypatch):
    body = plivo_dial(monkeypatch, 20)
    assert body["ring_timeout"] == 20
    assert body["to"] == CUSTOMER and body["from"] == NUMBER


def test_on_vobiz_rings_for_the_configured_seconds(monkeypatch):
    payload = vobiz_dial(monkeypatch, 20)
    assert payload["ring_timeout"] == "20"
    assert "hangup_on_ring" not in payload
    assert payload["to"] == CUSTOMER and payload["from"] == NUMBER

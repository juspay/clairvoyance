"""A XyneSpaces flow_action webhook all the way through the real door.

The DB-backed proof is test_ingress_integration.py (real Postgres, real SQL
filing). This file is the complementary, runnable-ANYWHERE spine: it mounts
the REAL webhook router on a FastAPI app exactly as app/crm/api.py does,
registers the REAL xyne bay into the INGRESS slot, walks a signed flow_action
submission via the REAL verify_signature -> letters parse, and captures the
EVENT the door would file — proving the merchant-side resolution (against the
merchant's OWN merchant_http/xyne door) and the flow_action letter itself,
without needing a database.

What this file pins is xyne's OWN seam: a documented, plainly-signed callback
reaching the event spine as a flow_action letter owned by the merchant whose
door sent the form; a merchant with no such door being dropped; and the
AppActionResponse the door answers with (finding 5/6).
"""

import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.crm.connectivity.providers.xyne import inbound as xyne
from app.crm.connectivity.schemas.connector import ConnectorInstallation
from app.crm.connectivity.topics import TOPIC_FLOW_ACTION
from app.crm.record import api as record_api
from app.crm.record.ingress import INGRESS, register_ingress

SECRET = "xyne-flow-secret-for-tests"
MERCHANT = "flow-shop"
MESSAGE_ID = "e9be4e96-9e96-4c9e-b259-ee75ea8bb499"
URL = "/ingest/webhooks/xyne"


def _signed(body: bytes) -> dict:
    """The relay's headers over these exact bytes — PLAIN HEX per Xyne's
    Apps guide."""
    mac = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "x-xyne-signature": mac,
        "Content-Type": "application/json",
    }


def _submission(message_id: str = MESSAGE_ID, merchant: str = MERCHANT) -> bytes:
    """The relay's documented flow_action submission, serialised ONCE — the
    same bytes that get signed and posted, because re-serialising would
    invalidate the signature and turn the test into a 403. Merchant and phone
    come back from what WE sent in context.flowJSON.data."""
    return json.dumps(
        {
            "actionId": "submit-interview-availability",
            "type": "submit",
            "values": {
                "selected_dates": ["2026-10-15", "2026-10-16"],
                "slots_2026-10-15": ["10:00"],
                "slots_2026-10-16": ["09:00", "10:00"],
            },
            "context": {
                "flowJSON": {
                    "version": "2.0",
                    "screenId": "interview-availability",
                    "data": {
                        "merchant_id": merchant,
                        "customer_mobile_number": "919876543210",
                        "event": {
                            "pm_name": "PM_name",
                            "stage": "Hackathon",
                            "role": "frontend",
                        },
                    },
                },
                "messageId": message_id,
                "conversationId": "conversation-1",
                "userId": "user-1",
            },
        }
    ).encode()


class _Installations:
    """Fake connector-installation accessor: each merchant owns a
    merchant_http door named "xyne" — the door the HTTP step sends through
    and against which the reply's merchant is verified."""

    def __init__(self) -> None:
        self.owners = {MERCHANT}

    async def get_installation_for_connector(
        self, merchant_id, connector_key, door_name=None
    ):
        if connector_key != "merchant_http" or door_name != "xyne":
            return None
        if merchant_id not in self.owners:
            return None
        return ConnectorInstallation(
            id=f"i-{merchant_id}",
            merchant_id=merchant_id,
            connector_key="merchant_http",
            external_account_id="door-1",
            door_name="xyne",
            status="healthy",
        )


@pytest.fixture
def secret(monkeypatch):
    """The platform secret present — the normal running state."""
    monkeypatch.setattr(xyne, "XYNE_WEBHOOK_SECRET", SECRET)


@pytest.fixture
def client(secret, monkeypatch) -> TestClient:
    """The real webhook door over the real xyne bay, with the spine captured.

    Mounted with prefix /ingest/webhooks exactly as app/crm/api.py mounts
    it, and the REAL xyne spec registered under its production key — so a
    dropped include_router or a missing register_ingress fails here, not in
    the field.
    """
    from app.crm.connectivity import contracts as connectivity_contracts

    register_ingress("xyne", connectivity_contracts.XYNE_INGRESS)
    monkeypatch.setattr(
        "app.crm.connectivity.ingress.installation_accessor", _Installations()
    )
    app = FastAPI()
    app.include_router(record_api.webhook_router, prefix="/ingest/webhooks")
    return TestClient(app)


@pytest.fixture
def filed(monkeypatch) -> list:
    """Captures every event the door files through ingest_event."""
    events: list = []
    cap = monkeypatch.setattr

    async def _ingest_event(**kwargs):
        events.append(kwargs)
        return f"ev-{len(events)}"

    cap(record_api, "ingest_event", _ingest_event)
    return events


def test_the_xyne_bay_is_registered_under_its_key() -> None:
    """The composition root registered the real xyne bay under its key, with a
    no-op handshake (Xyne has none) and the AppActionResponse answers."""
    import app.crm.api  # noqa: F401  (import triggers register_ingress)

    spec = INGRESS.get("xyne")
    assert spec is not None
    assert spec.verify is xyne.verify_signature
    assert spec.envelope is not None
    assert spec.challenge({}) is None
    assert spec.success_reply is not None
    assert spec.error_reply is not None


def test_the_url_we_give_xyne_is_really_mounted(client, filed) -> None:
    """The URL the relay is given is really mounted on the real router."""
    body = _submission()
    assert client.post(URL, content=body, headers=_signed(body)).status_code == 200


def test_a_signed_flow_action_submission_files_a_flow_action_event(
    client, filed
) -> None:
    """A signed flow_action submission comes through as a flow_action event
    owned by the merchant whose door sent the form, and answered with a
    close_screen AppActionResponse (finding 5)."""
    body = _submission()
    response = client.post(URL, content=body, headers=_signed(body))
    assert response.status_code == 200
    assert response.json() == {
        "type": "close_screen",
        "finalMessage": "✅ Thanks, your response is saved",
    }
    assert len(filed) == 1
    letter = filed[0]
    assert letter["merchant_id"] == MERCHANT
    assert letter["source"] == "xyne"
    assert letter["topic"] == TOPIC_FLOW_ACTION
    assert letter["external_id"] == MESSAGE_ID
    # The relay's whole submission rides verbatim.
    payload = letter["payload"]
    assert payload["actionId"] == "submit-interview-availability"
    assert payload["values"]["slots_2026-10-15"] == ["10:00"]
    # The flowJSON version, a constant (finding 9), never an env setting.
    assert letter["schema_version"] == xyne.SCHEMA_VERSION


def test_a_wrong_signature_is_403_and_files_nothing(client, filed) -> None:
    """A callback that cannot sign is refused before its body is trusted."""
    body = _submission()
    headers = _signed(body)
    headers["x-xyne-signature"] = "f" * 64
    response = client.post(URL, content=body, headers=headers)
    assert response.status_code == 403
    assert filed == []


def test_a_merchant_with_no_xyne_door_files_nowhere(client, filed) -> None:
    """A callback naming a merchant that owns no merchant_http/xyne door is
    accepted (200) but filed nowhere — filing it under a merchant whose door
    never sent the form would be a cross-tenant write."""
    body = _submission(merchant="flow-shop-nobody")
    response = client.post(URL, content=body, headers=_signed(body))
    assert response.status_code == 200
    assert filed == []


def test_the_xyne_bay_has_no_handshake(client) -> None:
    """Xyne has no subscription handshake, so the GET door answers 404 — the
    same silence as an unknown provider (no challenge to echo)."""
    response = client.get(URL, params={"verify_token": "x", "challenge": "abc-1"})
    assert response.status_code == 404


def test_a_store_failure_answers_the_json_error_reply(
    client, filed, monkeypatch
) -> None:
    """If storing the letter fails, the door answers the error AppActionResponse
    (200 with an error JSON the person sees and can resubmit) — never a silent
    503, because Xyne has no automatic retry (finding 6)."""

    async def _boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(record_api, "ingest_event", _boom)
    body = _submission()
    response = client.post(URL, content=body, headers=_signed(body))
    assert response.status_code == 200
    assert response.json() == {
        "type": "error",
        "message": "Couldn't save your response, please submit again",
    }
    assert filed == []

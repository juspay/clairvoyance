"""A XyneSpaces flow_action webhook all the way through the real door.

The DB-backed proof is test_ingress_integration.py (real Postgres, real SQL
filing) and Xyne's own DB tests live in that file beside Meta's. This file
is the complementary, runnable-ANYWHERE spine: it mounts the REAL webhook
router on a FastAPI app exactly as app/crm/api.py does, registers the REAL
xyne bay into the INGRESS slot, walks a signed flow_action submission
through the REAL verify_signature -> letters parse, and captures the EVENT
the door would file — proving the merchant-side resolution and the
flow_action letter itself, without needing a database.

The consumer/workflow side (a plan keyed on entry.topic="flow_action"
running off the filed event) is driver-tested for every topic by
test_worker_runtime.py; what this file pins is xyne's OWN seam: the relay's
response reaching the event spine as a flow_action letter owned by the
right merchant.
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
TOKEN = "xyne-flow-token-for-tests"
ACCOUNT = "flow-account-xyne-1"
ACCOUNT2 = "flow-account-xyne-2"
MERCHANT = "flow-shop"
MERCHANT2 = "flow-shop-2"
EVENT_ID = "ev_0001"
TS = "1788177600"
URL = "/ingest/webhooks/xyne"


def _signed(body: bytes) -> dict:
    """The relay's headers over these exact bytes."""
    mac = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "x-xyne-signature": f"sha256={mac}",
        "Content-Type": "application/json",
    }


def _submission() -> bytes:
    """The relay's flow_action submission, serialised ONCE — the same bytes
    that get signed and posted, because re-serialising would invalidate the
    signature and turn the test into a 403."""
    return json.dumps(
        {
            "flow_action": {
                "type": "flow_action",
                "id": EVENT_ID,
                "account_id": ACCOUNT,
                "performed_at": TS,
            },
            "customer_mobile_number": "919876543210",
            "customer_name": "Ada",
            "values": {
                "selected_dates": ["2026-10-15", "2026-10-16"],
                "slots_2026-10-15": ["10:00"],
                "slots_2026-10-16": ["09:00", "10:00"],
            },
        }
    ).encode()


class _Installations:
    """Fake connector-installation accessor: each flow account is owned by its
    own merchant, matching the real lookup the ingress root performs."""

    async def get_installation_for_inbound(self, connector_key, external_account_id):
        owners = {ACCOUNT: MERCHANT, ACCOUNT2: MERCHANT2}
        merchant = owners.get(external_account_id)
        if merchant is None:
            return None
        return ConnectorInstallation(
            id="i-xyne",
            merchant_id=merchant,
            connector_key="xyne",
            external_account_id=external_account_id,
            status="healthy",
        )


@pytest.fixture
def secret(monkeypatch):
    """Both platform secrets present — the normal running state."""
    monkeypatch.setattr(xyne, "XYNE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(xyne, "XYNE_WEBHOOK_VERIFY_TOKEN", TOKEN)


@pytest.fixture
def client(secret, monkeypatch) -> TestClient:
    """The real webhook door over the real xyne bay, with the spine captured.

    Mounted with prefix /ingest/webhooks exactly as app/crm/api.py mounts
    it, and the REAL xyne spec registered under its production key — so a
    dropped include_router or a missing register_ingress fails here, not in
    the field.
    """
    # The one registration line, exactly as the composition root writes it
    # (app/crm/api.py: register_ingress("xyne", contracts.XYNE_INGRESS)).
    # Importing app.crm.api also registers it, but spelling it here keeps the
    # test independent of that module's unrelated routers.
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
    """The composition root registered the real xyne bay under its key."""
    # app.crm.api is the production root that fills the slot — importing it
    # is the same registration the running service performs at startup.
    import app.crm.api  # noqa: F401  (import triggers register_ingress)

    spec = INGRESS.get("xyne")
    assert spec is not None
    assert spec.verify is xyne.verify_signature
    assert spec.envelope is not None
    assert spec.challenge is xyne.handshake_challenge


def test_the_url_we_give_xyne_is_really_mounted(client, filed) -> None:
    """The URL the relay is given is really mounted on the real router."""
    assert (
        client.post(
            URL, content=_submission(), headers=_signed(_submission())
        ).status_code
        == 200
    )


def test_a_signed_flow_action_submission_files_a_flow_action_event(
    client, filed
) -> None:
    """A signed flow_action submission comes through as a flow_action event
    owned by the merchant that installed the XYNE connector."""
    response = client.post(URL, content=_submission(), headers=_signed(_submission()))
    assert response.status_code == 200
    assert len(filed) == 1
    letter = filed[0]
    assert letter["merchant_id"] == MERCHANT
    assert letter["source"] == "xyne"
    assert letter["topic"] == TOPIC_FLOW_ACTION
    assert letter["external_id"] == f"{ACCOUNT}:{EVENT_ID}"
    # The relay's whole submission rides verbatim — the spine's extractor
    # reads the declared fields off it unchanged.
    payload = letter["payload"]
    assert payload["flow_action"]["id"] == EVENT_ID
    assert payload["values"]["slots_2026-10-15"] == ["10:00"]
    # Canon T13 col 5: the platform's stamped webhook schema version, never
    # an inferred "1".
    from app.core.config.static import XYNE_WEBHOOK_SCHEMA_VERSION

    assert letter["schema_version"] == XYNE_WEBHOOK_SCHEMA_VERSION


def test_a_wrong_signature_is_403_and_files_nothing(client, filed) -> None:
    """A callback that cannot sign is refused before its body is trusted."""
    body = _submission()
    headers = _signed(body)
    headers["x-xyne-signature"] = "sha256=" + "f" * 64
    response = client.post(URL, content=body, headers=headers)
    assert response.status_code == 403
    assert filed == []


def test_a_flow_account_no_merchant_owns_files_nowhere(client, filed) -> None:
    """A flow account nobody owns is accepted (200) but filed nowhere —
    filing it under any merchant would be a cross-tenant leak."""
    body = json.dumps(
        {
            "flow_action": {
                "type": "flow_action",
                "id": "ev-orphan",
                "account_id": "flow-account-nobody",
                "performed_at": TS,
            }
        }
    ).encode()
    response = client.post(URL, content=body, headers=_signed(body))
    assert response.status_code == 200
    assert filed == []


def test_the_handshake_echoes_the_challenge_over_http(client) -> None:
    """The relay's subscription challenge echoes back once, raw."""
    response = client.get(
        URL,
        params={"mode": "subscribe", "verify_token": TOKEN, "challenge": "abc-1"},
    )
    assert response.status_code == 200
    assert response.text == "abc-1"


# --- the STANDALONE shape: a stamped account_id, no flow_action wrapper ----


def _standalone_submission(account: str = ACCOUNT, event: str = "msg-standalone"):
    """A Xyne 'postMessage' response carried as a webhook: no flow_action
    wrapper, but the door stamps a top-level account_id so the letter can be
    owned by the flow/installation account the merchant installed."""
    return json.dumps(
        {
            "actionId": "submit-interview-availability",
            "type": "submit",
            "account_id": account,
            "values": {
                "selected_dates": ["2026-10-15"],
                "slots_2026-10-15": ["10:00"],
            },
            "context": {
                "messageId": event,
                "conversationId": "conv-standalone",
                "userId": "user-standalone",
            },
        }
    ).encode()


def test_a_standalone_response_with_a_stamped_account_id_files_a_flow_action_event(
    client, filed
) -> None:
    """A standalone (unwrapped) submission names its owner through the
    stamped top-level account_id and files a flow_action letter for the
    merchant that installed that flow account."""
    response = client.post(
        URL, content=_standalone_submission(), headers=_signed(_standalone_submission())
    )
    assert response.status_code == 200
    assert len(filed) == 1
    letter = filed[0]
    assert letter["merchant_id"] == MERCHANT
    assert letter["source"] == "xyne"
    assert letter["topic"] == TOPIC_FLOW_ACTION
    # The event id falls back to the submission's own message id, so dedupe
    # on the spine keys on something the sender actually names.
    assert letter["external_id"] == f"{ACCOUNT}:msg-standalone"
    payload = letter["payload"]
    assert payload["values"]["slots_2026-10-15"] == ["10:00"]
    assert payload["context"]["messageId"] == "msg-standalone"


def test_a_standalone_response_without_an_account_id_files_nowhere(
    client, filed
) -> None:
    """A standalone submission the door cannot attribute is accepted (200)
    but filed nowhere — spelling who owns it is the one thing the stamped
    account_id buys, and absent the flow account we must not guess a
    merchant."""
    body = _standalone_submission(account="")
    response = client.post(URL, content=body, headers=_signed(body))
    assert response.status_code == 200
    assert filed == []


def test_a_standalone_response_payload_still_decodes_for_the_workflow(
    client, filed
) -> None:
    """The whole standalone body rides verbatim, so the spine's flat
    extractor reads the declared payload.* paths and the selected_slots
    derive off it unchanged — the same decode test_xyne_extractor pins for
    the enveloped shape, now for the standalone one."""
    from app.crm.record import catalog
    from app.crm.record.extractors import engine

    response = client.post(
        URL, content=_standalone_submission(), headers=_signed(_standalone_submission())
    )
    assert response.status_code == 200
    spec = catalog.code_spec("xyne", "flow_action")
    assert spec is not None
    extracted = engine.extract(filed[0]["payload"], spec)
    assert extracted.variables["selected_slots"] == "2026-10-15: 10:00"
    assert extracted.variables["actionId"] == "submit-interview-availability"
    assert extracted.variables["messageId"] == "msg-standalone"


# --- the GENERIC face: every merchant, JSON payload, stored errors -----------


def _multi_account_submission() -> bytes:
    """One signed response that carries facts for TWO flow accounts at once —
    a postMessage batch — no flow_action wrapper, each item naming its own
    owner and event id."""
    return json.dumps(
        {
            "actionId": "submit-interview-availability",
            "type": "submit",
            "items": [
                {
                    "account_id": ACCOUNT,
                    "id": "ev-merchant-1",
                    "values": {"selected_dates": ["2026-10-15"]},
                },
                {
                    "account_id": ACCOUNT2,
                    "id": "ev-merchant-2",
                    "values": {"selected_dates": ["2026-10-16"]},
                },
            ],
        }
    ).encode()


def test_a_batched_response_files_one_letter_per_merchant(client, filed) -> None:
    """A response referencing several flow accounts resolves to one letter per
    merchant that owns one — no owner is missed or merged, whatever the batch
    shape."""
    body = _multi_account_submission()
    response = client.post(URL, content=body, headers=_signed(body))
    assert response.status_code == 200
    assert len(filed) == 2
    by_owner = {letter["merchant_id"]: letter for letter in filed}
    assert set(by_owner) == {MERCHANT, MERCHANT2}
    assert by_owner[MERCHANT]["external_id"] == f"{ACCOUNT}:ev-merchant-1"
    assert by_owner[MERCHANT2]["external_id"] == f"{ACCOUNT2}:ev-merchant-2"
    # Each letter still carries the WHOLE response body as its payload — every
    # merchant keeps the sender's verbatim object, so no item is lost.
    for letter in filed:
        assert letter["payload"]["items"][0]["account_id"] == ACCOUNT


def _failed_submission() -> bytes:
    """A signed response that reports a failure to the sender — errormsg at
    the top level and an error code in the flow_action container."""
    return json.dumps(
        {
            "flow_action": {
                "id": EVENT_ID,
                "account_id": ACCOUNT,
                "performed_at": TS,
            },
            "errormsg": "Out of stock for selected dates",
            "error": "inventory_unavailable",
        }
    ).encode()


def test_an_error_response_stores_errormsg_and_error_in_the_payload(
    client, filed
) -> None:
    """A response that reports a failure has its errormsg/error aggregated
    into the stored payload under one documented key — a consumer reads
    xyne_errors, never the relay's spelling — while the response body itself
    is still stored untouched."""
    body = _failed_submission()
    response = client.post(URL, content=body, headers=_signed(body))
    assert response.status_code == 200
    assert len(filed) == 1
    payload = filed[0]["payload"]
    # The response is still stored verbatim.
    assert payload["errormsg"] == "Out of stock for selected dates"
    # And its failure is filed where a consumer can read it.
    assert payload[xyne.PAYLOAD_ERRORS_KEY] == {
        "errormsg": "Out of stock for selected dates",
        "error": "inventory_unavailable",
    }


def test_the_stored_payload_is_always_a_json_object(client, filed) -> None:
    """A signed response that is a bare JSON string (no object) is kept as the
    payload rather than stored as a quoted string — the jsonb payload is
    always an object, however oddly-shaped the response is."""
    body = json.dumps(
        {
            "flow_action": {"id": "ev-str", "account_id": ACCOUNT, "performed_at": TS},
            # A string-typed error leaf, not an object.
            "error": "transient relay failure",
        }
    ).encode()
    response = client.post(URL, content=body, headers=_signed(body))
    assert response.status_code == 200
    assert len(filed) == 1
    payload = filed[0]["payload"]
    # Still a dict — a real object the spine can read, never a quoted string.
    assert isinstance(payload, dict)
    assert payload[xyne.PAYLOAD_ERRORS_KEY]["error"] == "transient relay failure"

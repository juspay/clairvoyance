"""Xyne, end to end: the event it files, the per-app webhook door, and the
connector (onboarding an app as ``xyne:<appId>`` and ``post_message``).

Three sections, one per layer. Events run the real extractor over the
recorded submission in tests/crm/fixtures/xyne/. Webhook tests pin raw-body
verification and parsing with no database, then run the real ingress root and
the real /crm route with only the installation lookup, the vault read and
event storage stubbed. Connector tests can HTTP through doubles.stub_http on
the shared Xyne client. Real Postgres filing is test_ingress_integration's.
"""

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.crm.record.extractors.xyne as xyne_spec
from app.crm.connectivity import accounts, ingress as connectivity_ingress
from app.crm.connectivity.actions import action_names, validate_action_args
from app.crm.connectivity.connectors import CONNECTORS
from app.crm.connectivity.providers.base import ActionError, ConnectorHandshakeError
from app.crm.connectivity.providers.xyne import (
    actions as xyne_actions,
    client as xyne_client,
    inbound,
    onboard,
)
from app.crm.connectivity.providers.xyne.onboard import (
    OnboardXyneRequest,
    XyneOnboarder,
)
from app.crm.connectivity.schemas.connector import ConnectorInstallation
from app.crm.connectivity.schemas.ingress import OWNER_ACCOUNT
from app.crm.connectivity.schemas.message import CredentialBundle
from app.crm.connectivity.topics import TOPIC_FLOW_ACTION
from app.crm.outreach import plans
from app.crm.outreach.catalog_laws import Catalogs
from app.crm.record import api as record_api, catalog
from app.crm.record.extractors import EXTRACTORS, engine
from app.crm.record.ingress import BAY_INGRESS
from app.crm.record.schemas import CatalogEntry
from tests.crm.doubles import stub_http

# ============================================================================
# Events: the flow_action catalog entry, identity, {selected_slots}, workflow catalog laws
# ============================================================================
FIXTURE = Path(__file__).parent / "fixtures" / "xyne" / "flow_action.json"

FLOW_SPEC = catalog.code_spec("xyne", "flow_action")
TOPIC = "flow_action"


def _fixture() -> Dict[str, Any]:
    """The recorded relay submission, byte-identical to the fixture."""
    return json.loads(FIXTURE.read_text())


def _extract(payload: Dict[str, Any]):
    """The engine over xyne's flow_action spec — the worker's exact read."""
    assert FLOW_SPEC is not None
    return engine.extract(payload, FLOW_SPEC)


# --- the package's public surface is the registry, nothing else ----------------


def test_the_package_exports_the_spec_contract_and_nothing_else() -> None:
    """SOURCE · ENTRIES · DERIVERS are what record/catalog.py reads off a
    SPEC_MODULES entry — and ALL the package exports."""
    assert xyne_spec.__all__ == ["SOURCE", "ENTRIES", "DERIVERS"]


# --- the decode: customer-level, a phone handle, clean variables --------------


def test_flow_action_is_declared_about_a_customer() -> None:
    """The submission names the CUSTOMER who filled the form — the identity
    field is a phone we sent in flowJSON.data, so it resolves to the person
    the run is about (finding 3)."""
    spec = catalog.code_spec("xyne", "flow_action")
    assert spec is not None and spec.about == "customer"
    assert (
        "payload.context.flowJSON.data.customer_mobile_number" in spec.identity["phone"]
    )


def test_the_fixture_decodes_to_a_customer_and_a_phone_handle() -> None:
    """The engine over the recorded submit yields the phone handle it sent, so
    resolve() can find the same customer the run is for."""
    extracted = _extract(_fixture())
    assert extracted.about == "customer"
    assert extracted.handles.get("phone") == "+919876543210"


def test_a_submission_without_the_sent_phone_yields_no_handle() -> None:
    """A callback whose flowJSON.data carries no customer_mobile_number has no
    identity — the pass quarantines it as replayable, exactly as canon says a
    customer-level letter with no handle must be."""
    payload = _fixture()
    del payload["context"]["flowJSON"]["data"]["customer_mobile_number"]
    extracted = _extract(payload)
    assert extracted.about == "customer"
    assert extracted.handles == {}


def test_the_action_and_envelope_variables_ride_out_for_templates() -> None:
    """A workflow can read the action name, the context envelope, and the
    phone the sending plan wrote into the form."""
    extracted = _extract(_fixture())
    assert extracted.variables["actionId"] == "submit-interview-availability"
    assert extracted.variables["type"] == "submit"
    assert extracted.variables["kind"] == "interview_availability"
    # The merchant is the app's owner (the per-app webhook door), never a
    # value read from the body.
    assert "merchant_id" not in extracted.variables
    assert extracted.variables["customer_mobile_number"] == "919876543210"
    assert extracted.variables["messageId"] == "e9be4e96-9e96-4c9e-b259-ee75ea8bb499"
    assert (
        extracted.variables["conversationId"] == "ead0b5ff-32f9-460c-92d6-f52847257465"
    )
    assert extracted.variables["userId"] == "cmo014zxi0dz1rkq60eouxllf"


def test_the_selected_slots_derive_is_person_readable() -> None:
    """The date-named slot keys become ONE string a call node can read aloud —
    one line per date, in the recipient's own spelling, nothing reworded."""
    extracted = _extract(_fixture())
    assert extracted.variables["selected_slots"] == (
        "2026-10-15: 10:00\n2026-10-16: 09:00, 10:00\n2026-10-17: 14:00"
    )


def test_a_date_nobody_picked_is_left_out() -> None:
    """A slot key the recipient left empty contributes no line."""
    payload = _fixture()
    payload["values"]["slots_2026-10-17"] = []
    extracted = _extract(payload)
    assert extracted.variables["selected_slots"] == (
        "2026-10-15: 10:00\n2026-10-16: 09:00, 10:00"
    )


def test_an_empty_submission_reads_no_selected_slots() -> None:
    """An empty submission is a completed form with nothing picked — the
    derived slot name is simply absent (a None derived never rides out; the
    run's context drops it before any template sees it)."""
    payload = {
        "actionId": "submit-interview-availability",
        "type": "submit",
        "values": {},
    }
    extracted = _extract(payload)
    assert "selected_slots" not in extracted.variables


def test_a_submission_with_no_values_object_reads_nothing() -> None:
    """A malformed envelope never raises: the declared context fields ride
    out, the declared list is said as None (a letter that made none), and the
    derived slot name is absent."""
    extracted = _extract(
        {"actionId": "submit-interview-availability", "type": "submit"}
    )
    assert extracted.variables["actionId"] == "submit-interview-availability"
    assert extracted.variables["type"] == "submit"
    assert extracted.variables["selected_dates"] is None
    assert "selected_slots" not in extracted.variables


def test_xyne_is_a_spec_source_not_an_imperative_extractor() -> None:
    """A source is in ONE of the two — the spec or the flat extractor, never
    both (test_catalog.py fails CI on the overlap)."""
    assert "xyne" not in EXTRACTORS


# --- workflows against the same code-layer catalog --------------------------


def _entry() -> CatalogEntry:
    """The code-layer flow_action entry declared by Xyne."""
    (entry,) = [e for e in catalog.code_entries() if e.topic == TOPIC]
    return entry


def _catalogs() -> Catalogs:
    """The code catalog used by the server's workflow validation."""
    return {
        entry.topic: {f.path: f for f in entry.fields}
        for entry in catalog.code_entries()
    }


def _board(
    nodes: List[Dict[str, Any]],
    *,
    entry_where: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """A minimal workflow entering on flow_action."""
    return {
        "entry": {"topic": TOPIC, **({"where": entry_where} if entry_where else {})},
        "nodes": nodes,
        "edges": [],
        "goal": {"topics": ["order.placed"]},
        "purpose_key": "xyne.interview.slots",
    }


def _send_node(**words: Any) -> Dict[str, Any]:
    node: Dict[str, Any] = {
        "id": "confirm",
        "type": "send",
        "channel": "whatsapp",
        "template": "t",
    }
    node.update(words)
    return node


def test_a_flow_action_board_validates_against_the_code_catalog() -> None:
    """Declared variables and entry filters pass the workflow catalog laws."""
    board = _board(
        [_send_node(variables={"1": "selected_slots"})],
        entry_where=[
            {
                "field": "payload.actionId",
                "op": "is",
                "value": "submit-interview-availability",
            }
        ],
    )
    assert plans.validate_definition(board, catalogs=_catalogs()) == []


def test_a_where_on_a_declared_field_is_judged() -> None:
    """A filter on a declared flow_action field is legal."""
    board = _board(
        [_send_node(variables={"1": "selected_slots"})],
        entry_where=[{"field": "payload.type", "op": "is", "value": "submit"}],
    )
    assert plans.validate_definition(board, catalogs=_catalogs()) == []


def test_a_send_mapping_an_undeclared_field_is_refused() -> None:
    """An undeclared template variable is refused at publish time."""
    board = _board([_send_node(variables={"1": "loyalty_tier"})])
    problems = plans.validate_definition(board, catalogs=_catalogs())
    assert any("loyalty_tier" in p and "not a declared variable" in p for p in problems)


def test_a_where_on_an_undeclared_field_is_refused() -> None:
    """An undeclared entry filter cannot silently match nothing."""
    board = _board(
        [_send_node(variables={"1": "selected_slots"})],
        entry_where=[{"field": "totally_bogus_field", "op": "is", "value": "x"}],
    )
    problems = plans.validate_definition(board, catalogs=_catalogs())
    assert any("totally_bogus_field" in p for p in problems)


def test_an_unknown_topic_is_not_flow_action() -> None:
    """Unknown topics cannot supply fields for a flow_action workflow."""
    board = _board(
        [_send_node()], entry_where=[{"field": "payload.x", "op": "is", "value": "y"}]
    )
    board["entry"]["topic"] = "some.unseen_topic"
    problems = plans.validate_definition(board, catalogs=_catalogs())
    assert any("not in the catalog" in p for p in problems)


def test_the_variable_fields_a_board_can_map_are_the_declared_ones() -> None:
    """Workflow variables are exactly the fields declared by the Xyne spec."""
    declared = {f.path for f in _entry().fields if f.variable}
    assert declared == {
        "payload.actionId",
        "payload.type",
        "payload.values.selected_dates",
        "selected_slots",
        "payload.context.messageId",
        "payload.context.conversationId",
        "payload.context.userId",
        "payload.context.flowJSON.data.kind",
        "payload.context.flowJSON.data.customer_mobile_number",
    }


# ============================================================================
# Webhooks: per-app signatures, the letter, the per-app door, the real route
# ============================================================================
SECRET = "xyne-app-secret-for-tests"
APP_ID = "app-123"
MERCHANT = "flow-shop"
MESSAGE_ID = "e9be4e96-9e96-4c9e-b259-ee75ea8bb499"
URL = f"/ingest/webhooks/xyne/{APP_ID}"


def _sig(raw: bytes, secret: str = SECRET) -> str:
    """A valid x-xyne-signature: plain hex HMAC-SHA256 of these bytes."""
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _body(**overrides: Any) -> Dict[str, Any]:
    """A documented flow_action submission."""
    fields: Dict[str, Any] = {
        "actionId": "submit-interview-availability",
        "type": "submit",
        "values": {"selected_dates": ["2026-10-15"], "slots_2026-10-15": ["10:00"]},
        "context": {
            "flowJSON": {
                "version": "2.0",
                "screenId": "interview-availability",
                "data": {"customer_mobile_number": "919876543210"},
            },
            "messageId": MESSAGE_ID,
            "conversationId": "conversation-1",
            "userId": "user-1",
        },
    }
    fields.update(overrides)
    return fields


# --- the signature -----------------------------------------------------------


def test_a_correct_plain_hex_signature_passes() -> None:
    raw = FIXTURE.read_bytes()
    assert inbound.verify_signature(raw, {"X-Xyne-Signature": _sig(raw)}, SECRET)


def test_a_prefixed_signature_is_also_accepted() -> None:
    raw = b"{}"
    headers = {"x-xyne-signature": "sha256=" + _sig(raw)}
    assert inbound.verify_signature(raw, headers, SECRET)


@pytest.mark.parametrize(
    "raw,header,secret",
    [
        (b"tampered", _sig(b"real"), SECRET),  # different bytes
        (b"{}", _sig(b"{}", "another-apps-secret"), SECRET),  # another app's secret
        (b"{}", None, SECRET),  # no header
        (b"{}", "not-hex!!", SECRET),  # not hex
        (b"{}", "sha256=", SECRET),  # empty after the prefix
        (b"{}", _sig(b"{}"), None),  # no secret stored
        (b"{}", _sig(b"{}"), ""),
    ],
)
def test_anything_but_this_apps_signature_is_refused(raw, header, secret) -> None:
    """Fail closed, and never raise."""
    headers = {} if header is None else {"x-xyne-signature": header}
    assert not inbound.verify_signature(raw, headers, secret)


# --- the letter --------------------------------------------------------------


def test_a_submission_is_one_letter_owned_by_the_app() -> None:
    """The owner is the app id from the URL — an ACCOUNT owner — and the body
    rides verbatim."""
    body = _body()
    (letter,) = inbound.letters(body, APP_ID)
    assert letter.owner_kind == OWNER_ACCOUNT
    assert letter.owner_id == APP_ID
    assert letter.connector_key == "xyne"
    assert letter.source == "xyne"
    assert letter.topic == TOPIC_FLOW_ACTION
    assert letter.external_id == MESSAGE_ID
    assert letter.payload == body
    assert letter.occurred_at is None
    assert letter.schema_version == inbound.SCHEMA_VERSION


def test_a_merchant_named_in_the_body_is_ignored() -> None:
    """A body claiming a merchant changes nothing: the owner is still the app."""
    body = _body()
    body["context"]["flowJSON"]["data"]["merchant_id"] = "someone-else"
    (letter,) = inbound.letters(body, APP_ID)
    assert letter.owner_id == APP_ID


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "click"),
        ("actionId", ""),
        ("actionId", []),
        ("values", None),
        ("values", []),
        ("context", None),
    ],
)
def test_only_well_formed_submissions_become_letters(field, value) -> None:
    assert inbound.letters(_body(**{field: value}), APP_ID) == []


@pytest.mark.parametrize("value", [None, "", " ", 123, []])
def test_a_submission_needs_a_message_id(value) -> None:
    body = _body()
    body["context"]["messageId"] = value
    assert inbound.letters(body, APP_ID) == []


def test_the_secret_key_matches_what_onboarding_vaults() -> None:
    """ingress.py spells the bundle key itself (rule 11 keeps it from
    importing onboard.py); this pins the two spellings together."""
    assert connectivity_ingress._XYNE_WEBHOOK_SECRET == onboard.WEBHOOK_SECRET


# --- the per-app bay, with the reads stubbed ---------------------------------


def _door(status: str = "healthy", credential_id: Optional[str] = "cred-1"):
    return ConnectorInstallation(
        id="inst-1",
        merchant_id=MERCHANT,
        connector_key="xyne",
        external_account_id=APP_ID,
        credential_id=credential_id,
        status=status,
    )


class _Installations:
    """The app -> door lookup: one app, owned by MERCHANT."""

    def __init__(self, door: Optional[ConnectorInstallation]) -> None:
        self.door = door
        self.calls: List[tuple] = []

    async def get_installation_for_inbound(self, connector_key, external_account_id):
        self.calls.append((connector_key, external_account_id))
        if self.door is None or external_account_id != APP_ID:
            return None
        return self.door


@pytest.fixture
def installations(monkeypatch) -> _Installations:
    stub = _Installations(_door())
    monkeypatch.setattr(connectivity_ingress, "installation_accessor", stub)
    return stub


@pytest.fixture
def vault(monkeypatch) -> Dict[str, Any]:
    """The app's credential bundle; tests edit it, or set it to None to make
    the read refuse."""
    state: Dict[str, Any] = {"values": {"webhook_secret": SECRET}}

    async def _bundle_for(installation):
        if state["values"] is None:
            raise accounts.AccountError("credential is missing or unreadable")
        return CredentialBundle(values=state["values"])

    monkeypatch.setattr(accounts, "bundle_for", _bundle_for)
    return state


async def test_the_bay_verifies_with_the_apps_own_secret(installations, vault):
    raw = b'{"a": 1}'
    spec = connectivity_ingress.XYNE_INGRESS
    assert await spec.verify(APP_ID, raw, {"x-xyne-signature": _sig(raw)})
    assert installations.calls == [("xyne", APP_ID)]


async def test_an_unknown_app_is_refused_before_any_vault_read(installations, vault):
    vault["values"] = None  # would raise if it were read
    raw = b"{}"
    spec = connectivity_ingress.XYNE_INGRESS
    assert not await spec.verify("app-nobody", raw, {"x-xyne-signature": _sig(raw)})


@pytest.mark.parametrize("status", ["revoked", "disabled", "degraded", "connecting"])
async def test_an_unusable_door_is_refused(monkeypatch, vault, status):
    monkeypatch.setattr(
        connectivity_ingress, "installation_accessor", _Installations(_door(status))
    )
    raw = b"{}"
    spec = connectivity_ingress.XYNE_INGRESS
    assert not await spec.verify(APP_ID, raw, {"x-xyne-signature": _sig(raw)})


async def test_an_unreadable_credential_is_refused(installations, vault):
    vault["values"] = None
    raw = b"{}"
    spec = connectivity_ingress.XYNE_INGRESS
    assert not await spec.verify(APP_ID, raw, {"x-xyne-signature": _sig(raw)})


async def test_a_bundle_without_a_webhook_secret_is_refused(installations, vault):
    vault["values"] = {"api_token": "t"}
    raw = b"{}"
    spec = connectivity_ingress.XYNE_INGRESS
    assert not await spec.verify(APP_ID, raw, {"x-xyne-signature": _sig(raw)})


# --- the real route ----------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    import app.crm.api  # noqa: F401  (import registers the bays)

    app = FastAPI()
    app.include_router(record_api.webhook_router, prefix="/ingest/webhooks")
    return TestClient(app)


@pytest.fixture
def filed(monkeypatch) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []

    async def _ingest_event(**kwargs):
        events.append(kwargs)
        return f"ev-{len(events)}"

    monkeypatch.setattr(record_api, "ingest_event", _ingest_event)
    return events


def _post(client: TestClient, raw: bytes, url: str = URL, signature=None):
    headers = {
        "x-xyne-signature": signature or _sig(raw),
        "Content-Type": "application/json",
    }
    return client.post(url, content=raw, headers=headers)


def test_the_xyne_bay_is_registered_per_app() -> None:
    import app.crm.api  # noqa: F401

    assert BAY_INGRESS["xyne"] is connectivity_ingress.XYNE_INGRESS


def test_the_recorded_callback_files_under_the_apps_merchant(
    client, filed, installations, vault
) -> None:
    raw = FIXTURE.read_bytes()
    response = _post(client, raw)
    assert response.status_code == 200
    assert response.json() == {
        "type": "close_screen",
        "finalMessage": "Thanks, your response is saved",
    }
    (event,) = filed
    assert event["merchant_id"] == MERCHANT
    assert event["source"] == "xyne"
    assert event["topic"] == TOPIC_FLOW_ACTION
    assert event["external_id"] == json.loads(raw)["context"]["messageId"]
    assert event["payload"] == json.loads(raw)


def test_a_wrong_signature_is_403_and_files_nothing(
    client, filed, installations, vault
) -> None:
    raw = json.dumps(_body()).encode()
    assert _post(client, raw, signature="f" * 64).status_code == 403
    assert filed == []


def test_another_apps_url_does_not_accept_this_apps_signature(
    client, filed, installations, vault
) -> None:
    """The same signed bytes posted to an app nobody holds: 403, not 404 —
    the answer never says which app ids exist."""
    raw = json.dumps(_body()).encode()
    response = _post(client, raw, url="/ingest/webhooks/xyne/app-nobody")
    assert response.status_code == 403
    assert filed == []


def test_reserialised_json_fails_the_signature(
    client, filed, installations, vault
) -> None:
    raw = FIXTURE.read_bytes()
    response = client.post(
        URL, content=raw + b"\n", headers={"x-xyne-signature": _sig(raw)}
    )
    assert response.status_code == 403
    assert filed == []


def test_a_signed_off_shape_body_answers_the_error_reply(
    client, filed, installations, vault
) -> None:
    """Signed, but no letter in it: the person sees an error they can resubmit
    from, and nothing is filed."""
    raw = json.dumps(_body(type="click")).encode()
    response = _post(client, raw)
    assert response.status_code == 200
    assert response.json()["type"] == "error"
    assert filed == []


def test_a_store_failure_answers_the_error_reply(
    client, installations, vault, monkeypatch
) -> None:
    async def _boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(record_api, "ingest_event", _boom)
    raw = json.dumps(_body()).encode()
    response = _post(client, raw)
    assert response.status_code == 200
    assert response.json() == {
        "type": "error",
        "message": "Couldn't save your response, please submit again",
    }


def test_an_unknown_bayed_provider_is_404(client) -> None:
    assert _post(client, b"{}", url="/ingest/webhooks/nobody/app").status_code == 404


def test_the_shared_xyne_door_does_not_exist(client) -> None:
    """There is no platform-wide Xyne URL: every app posts to its own."""
    assert _post(client, b"{}", url="/ingest/webhooks/xyne").status_code == 404


# ============================================================================
# Connector: onboarding an app (xyne:<appId>) and post_message
# ============================================================================
TOKEN = "xyne-api-token"
ONBOARD_SECRET = "a-webhook-secret-of-length"
BASE = "https://spaces.xyne.juspay.net"


def _request(**overrides: Any) -> OnboardXyneRequest:
    fields: Dict[str, Any] = {
        "merchant_id": "flow-shop",
        "app_id": "app-123",
        "api_token": TOKEN,
        "webhook_secret": ONBOARD_SECRET,
    }
    fields.update(overrides)
    return OnboardXyneRequest(**fields)


# --- the registry ------------------------------------------------------------


def test_xyne_is_a_registered_door_with_no_pipe() -> None:
    spec = CONNECTORS["xyne"]
    assert spec.source == "xyne"
    assert spec.channel is None
    assert spec.templates is None
    assert spec.request_model is OnboardXyneRequest
    assert action_names("xyne") == ["post_message"]


# --- the onboarding request --------------------------------------------------


def test_the_base_url_defaults_to_xyne_spaces() -> None:
    assert _request().base_url == BASE


@pytest.mark.parametrize(
    "base_url,stored",
    [
        ("https://spaces.xyne.juspay.net/", BASE),
        ("https://other.xyne.juspay.net", "https://other.xyne.juspay.net"),
    ],
)
def test_a_xyne_host_is_accepted(base_url, stored) -> None:
    assert _request(base_url=base_url).base_url == stored


@pytest.mark.parametrize(
    "base_url",
    [
        "http://spaces.xyne.juspay.net",  # not https
        "https://evil.example.com",  # not a Xyne host
        "https://xyne.juspay.net.evil.com",  # suffix trick
        "https://user:pw@spaces.xyne.juspay.net",  # credentials in the URL
        "https://spaces.xyne.juspay.net/api",  # a path
        "https://169.254.169.254",  # metadata address
    ],
)
def test_any_other_base_url_is_refused(base_url) -> None:
    with pytest.raises(ValidationError):
        _request(base_url=base_url)


@pytest.mark.parametrize("app_id", ["", "a b", "../x", "a/b", "x" * 129])
def test_an_app_id_must_be_a_plain_token(app_id) -> None:
    with pytest.raises(ValidationError):
        _request(app_id=app_id)


def test_a_short_webhook_secret_is_refused() -> None:
    with pytest.raises(ValidationError):
        _request(webhook_secret="short")


def test_secrets_never_reach_a_repr() -> None:
    text = repr(_request())
    assert TOKEN not in text and ONBOARD_SECRET not in text


# --- the handshake -----------------------------------------------------------


async def test_a_good_token_becomes_a_healthy_door_with_its_bundle(
    monkeypatch,
) -> None:
    seen = stub_http(monkeypatch, xyne_client, lambda r: httpx.Response(200, json=[]))
    result = await XyneOnboarder().gather(_request())

    (call,) = seen
    assert call.method == "GET"
    assert str(call.url) == f"{BASE}/api/apps"
    assert call.headers["authorization"] == f"Bearer {TOKEN}"
    assert result.external_account_id == "app-123"
    assert result.address is None
    assert result.health_level == "healthy"
    # What onboarding.py vaults into the credentials table.
    assert result.bundle == {
        "base_url": BASE,
        "api_token": TOKEN,
        "webhook_secret": ONBOARD_SECRET,
    }


@pytest.mark.parametrize("code", [401, 403])
async def test_a_refused_token_is_a_handshake_error(monkeypatch, code) -> None:
    stub_http(monkeypatch, xyne_client, lambda r: httpx.Response(code))
    with pytest.raises(ConnectorHandshakeError, match="refused this API token"):
        await XyneOnboarder().gather(_request())


@pytest.mark.parametrize("code", [302, 429, 500, 503])
async def test_an_unreachable_xyne_writes_no_door(monkeypatch, code) -> None:
    stub_http(monkeypatch, xyne_client, lambda r: httpx.Response(code))
    with pytest.raises(ConnectorHandshakeError, match="could not be reached"):
        await XyneOnboarder().gather(_request())


def test_identify_knows_the_app_before_the_handshake() -> None:
    assert XyneOnboarder().identify(_request()) == ("app-123", None)


# --- post_message ------------------------------------------------------------


def _action_door(credential_id="cred-1") -> ConnectorInstallation:
    return ConnectorInstallation(
        id="inst-1",
        merchant_id="flow-shop",
        connector_key="xyne",
        external_account_id="app-123",
        credential_id=credential_id,
        status="healthy",
    )


@pytest.fixture
def action_vault(monkeypatch) -> Dict[str, Any]:
    state: Dict[str, Any] = {"values": {"base_url": BASE, "api_token": TOKEN}}

    async def _bundle_for(installation):
        if state["values"] is None:
            raise accounts.AccountError("credential is missing or unreadable")
        return CredentialBundle(values=state["values"])

    monkeypatch.setattr(xyne_actions, "bundle_for", _bundle_for)
    return state


def _args(**fields: Any) -> xyne_actions.PostMessageArgs:
    return xyne_actions.PostMessageArgs.model_validate({"channel_id": "ch-1", **fields})


async def _perform(args, installation=None) -> Dict[str, Any]:
    return await xyne_actions.PostMessage().perform(
        "flow-shop",
        installation if installation is not None else _action_door(),
        args,
        {"run_id": "run-1", "node_id": "ask"},
    )


async def test_post_message_sends_channel_text_and_flow(
    monkeypatch, action_vault
) -> None:
    seen = stub_http(
        monkeypatch,
        xyne_client,
        lambda r: httpx.Response(200, json={"messageId": "m-1"}),
    )
    flow = {"version": "2.0", "data": {"enrollment_id": "run-1"}}
    result = await _perform(_args(text="Pick slots", flow=flow))

    (call,) = seen
    assert call.method == "POST"
    assert str(call.url) == f"{BASE}/api/apps/chat/postMessage"
    assert call.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(call.content) == {
        "channelId": "ch-1",
        "text": "Pick slots",
        "flow": flow,
    }
    assert result == {"ok": True, "message_id": "m-1"}


async def test_a_4xx_parks_and_a_5xx_retries(monkeypatch, action_vault) -> None:
    stub_http(monkeypatch, xyne_client, lambda r: httpx.Response(404, text="nope"))
    with pytest.raises(ActionError, match="404"):
        await _perform(_args(text="hi"))

    stub_http(monkeypatch, xyne_client, lambda r: httpx.Response(503))
    with pytest.raises(RuntimeError):
        await _perform(_args(text="hi"))


async def test_an_unreadable_credential_parks(action_vault) -> None:
    action_vault["values"] = None
    with pytest.raises(ActionError, match="credential unusable"):
        await _perform(_args(text="hi"))


async def test_a_bundle_missing_the_token_parks(action_vault) -> None:
    action_vault["values"] = {"base_url": BASE}
    with pytest.raises(ActionError, match="api_token"):
        await _perform(_args(text="hi"))


async def test_a_stored_base_url_off_xyne_is_never_called(
    monkeypatch, action_vault
) -> None:
    """A hand-edited action_vault row cannot send the token anywhere else."""
    seen = stub_http(monkeypatch, xyne_client, lambda r: httpx.Response(200))
    action_vault["values"] = {
        "base_url": "https://evil.example.com",
        "api_token": TOKEN,
    }
    with pytest.raises(ActionError):
        await _perform(_args(text="hi"))
    assert seen == []


def test_post_message_needs_a_channel_and_something_to_say() -> None:
    assert validate_action_args("xyne", "post_message", {"channel_id": "c"}) != []
    assert validate_action_args("xyne", "post_message", {"text": "hi"}) == [
        "channel_id"
    ]
    assert (
        validate_action_args("xyne", "post_message", {"channel_id": "c", "text": "x"})
        == []
    )

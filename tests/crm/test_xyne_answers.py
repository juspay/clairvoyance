"""Xyne form answers without a database: the inbound face (signature, which
callbacks become letters, the event id), the per-account bay's answers, the
Xyne bay's verify, and the catalog entry the event worker decodes through.

The same path against a real Postgres and the real route:
test_xyne_answers_integration.py.
"""

import copy
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.crm.connectivity.ingress as connectivity_ingress
from app.crm.connectivity.accounts import installations
from app.crm.connectivity.providers.xyne import inbound, onboard
from app.crm.connectivity.schemas.connector import ConnectorInstallation
from app.crm.connectivity.schemas.message import CredentialBundle
from app.crm.connectivity.vocabulary import topics
from app.crm.record import api as record_api, catalog, ingress
from app.crm.record.extractors import engine, forms
from app.crm.record.extractors.xyne import flow_action
from app.crm.record.schemas import EventIn

FIXTURE = Path(__file__).parent / "fixtures" / "xyne" / "flow_action.json"
SECRET = "app-hire01-signing-secret"
APP_ID = "app_hire01"
MERCHANT = "pragathi"
FLOW_ACTION = {"X-Xyne-Event": "flow_action"}


def _body() -> Dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _raw(body: Dict[str, Any]) -> bytes:
    return json.dumps(body, separators=(",", ":")).encode()


def _sign(raw: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


# --- the signature (Xyne's signWebhookPayload: plain-hex HMAC-SHA256) --------------


def test_a_body_signed_with_the_apps_secret_is_genuine() -> None:
    raw = _raw(_body())
    assert inbound.verify_signature(raw, {"X-Xyne-Signature": _sign(raw)}, SECRET)


def test_the_header_name_is_not_case_sensitive() -> None:
    raw = _raw(_body())
    assert inbound.verify_signature(raw, {"x-xyne-signature": _sign(raw)}, SECRET)


@pytest.mark.parametrize(
    "headers,secret",
    [
        ({}, SECRET),  # unsigned (Xyne's commands are, and are refused here)
        ({"X-Xyne-Signature": ""}, SECRET),
        ({"X-Xyne-Signature": "not-hex"}, SECRET),
        ({"X-Xyne-Signature": "sha256=" + "0" * 64}, SECRET),
        ({"X-Xyne-Signature": "é" * 8}, SECRET),
        ({"X-Xyne-Signature": "SIGNED"}, None),  # no secret on file
        ({"X-Xyne-Signature": "SIGNED"}, "another-apps-secret"),
    ],
)
def test_anything_else_is_not_genuine(headers: Dict[str, str], secret: Any) -> None:
    raw = _raw(_body())
    headers = {k: (_sign(raw) if v == "SIGNED" else v) for k, v in headers.items()}
    assert not inbound.verify_signature(raw, headers, secret)


def test_the_signature_covers_the_bytes_not_the_meaning() -> None:
    """Re-serialising a body changes its bytes; the MAC is over what was
    sent, so a body re-written by anyone after signing fails."""
    raw = _raw(_body())
    edited = raw.replace(b"10:00", b"09:00")
    assert not inbound.verify_signature(
        edited, {"X-Xyne-Signature": _sign(raw)}, SECRET
    )


# --- which callbacks become a letter -------------------------------------------------


def test_a_submit_is_one_letter_owned_by_the_app_in_the_url() -> None:
    [letter] = inbound.letters(_body(), FLOW_ACTION, APP_ID)
    assert letter.owner_kind == "account" and letter.owner_id == APP_ID
    assert (letter.source, letter.topic) == ("xyne", "flow_action")
    assert letter.connector_key == "xyne"
    assert letter.payload == _body()  # filed as sent, flowJSON and all
    assert letter.occurred_at is None  # Xyne sends no time: record stamps it
    assert letter.external_id.startswith("cmh3b7r2t000aqz8m4n1x8k5p:")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Xyne-Event": "command"},
        {"X-Xyne-Event": "shortcut"},
        {"X-Xyne-Event": ""},
    ],
)
def test_only_a_flow_action_callback_is_filed(headers: Dict[str, str]) -> None:
    """App events (no header) and commands are genuine but not filed yet —
    each becomes one handler the day a merchant needs it."""
    assert inbound.letters(_body(), headers, APP_ID) == []


def test_a_field_edit_is_never_filed() -> None:
    body = _body()
    body["type"] = "inputChange"
    assert inbound.letters(body, FLOW_ACTION, APP_ID) == []


@pytest.mark.parametrize(
    "break_it",
    [
        lambda b: b.pop("actionId"),
        lambda b: b.update(actionId="  "),
        lambda b: b.update(values=["10:00"]),
        lambda b: b.pop("context"),
        lambda b: b["context"].pop("messageId"),
        lambda b: b["context"].update(messageId=""),
        lambda b: b.update(type="SUBMIT"),
    ],
)
def test_a_submit_not_shaped_like_xynes_is_not_guessed_onto_the_spine(
    break_it: Any,
) -> None:
    body = _body()
    break_it(body)
    assert inbound.letters(body, FLOW_ACTION, APP_ID) == []


def test_the_same_answer_twice_is_one_event_and_a_changed_answer_is_another() -> None:
    """Xyne never retries, so a second callback on one form is the person
    resubmitting: the same answer dedupes, a changed one is kept."""
    body = _body()
    first = inbound.letters(body, FLOW_ACTION, APP_ID)[0].external_id
    reordered = copy.deepcopy(body)
    reordered["values"] = dict(reversed(list(body["values"].items())))
    assert inbound.letters(reordered, FLOW_ACTION, APP_ID)[0].external_id == first
    changed = copy.deepcopy(body)
    changed["values"]["slots_2026-10-30"] = ["11:00"]
    assert inbound.letters(changed, FLOW_ACTION, APP_ID)[0].external_id != first


def test_the_same_answer_from_another_person_is_another_event() -> None:
    """A form posted in a channel can be answered by any member. Two people
    picking the same slots on one form are two answers — keyed on the
    message and the values alone, the second was dropped as a duplicate
    (found by the end-to-end run)."""
    body = _body()
    first = inbound.letters(body, FLOW_ACTION, APP_ID)[0].external_id
    other = copy.deepcopy(body)
    other["context"]["userId"] = "cmh2xa04m0007qz8l1v6d9b3c"
    assert inbound.letters(other, FLOW_ACTION, APP_ID)[0].external_id != first


# --- spellings pinned across the boundary --------------------------------------------


def test_the_topic_is_spelled_the_same_on_both_sides() -> None:
    assert topics.TOPIC_FLOW_ACTION == flow_action.TOPIC == "flow_action"


def test_no_topic_is_declared_by_two_code_sources() -> None:
    """A door names its topic alone, so a topic two sources declare cannot be
    validated (record/catalog.py, AmbiguousTopic). Filing Xyne answers as
    message.inbound would have made every WhatsApp-reply plan
    unpublishable — this is the guard that keeps the next source honest."""
    seen: Dict[str, str] = {}
    for source, topic in catalog.CATALOG:
        assert topic not in seen, f"{topic}: declared by {seen.get(topic)} and {source}"
        seen[topic] = source


def test_the_secret_key_is_the_one_onboarding_vaults() -> None:
    assert inbound.WEBHOOK_SECRET_KEY == onboard.WEBHOOK_SECRET == "webhook_secret"


def test_no_xyne_variable_is_a_name_the_walker_keeps_for_itself() -> None:
    """A run drops every key the walker keeps for its own bookkeeping
    (phone, reply_*, action_*, message_*, …), so a variable spelled like one
    is declared, extracted — and never reaches a step. `action_id` was one.
    (WhatsApp's message_text has the same problem today; out of this PR.)"""
    from app.crm.outreach.nodes.context import is_bookkeeping

    entry = catalog.CATALOG[("xyne", "flow_action")]
    names = [engine.variable_name(f.path) for f in entry.fields if f.variable]
    assert names and [n for n in names if is_bookkeeping(n)] == []


def test_the_answer_reaches_the_run_not_only_the_extractor() -> None:
    """The run's own filter, applied to what the engine extracted: every
    variable a plan can name survives into the run's context."""
    from app.crm.outreach.entry import _context_from_payload

    variables = _extract(_body()).variables
    kept = _context_from_payload(variables, 1000)
    assert {"reply", "button", "flow_response"} <= set(kept)
    assert kept["button"] == "submit-interview-availability"


def test_the_word_is_the_one_whatsapp_forms_answer_with() -> None:
    assert flow_action.reply(_body()) == forms.FORM_SUBMITTED == "form_submitted"


# --- the catalog entry, through the decode engine ------------------------------------


def _extract(body: Dict[str, Any]) -> Any:
    spec = engine.spec_for_entry(
        catalog.CATALOG[("xyne", "flow_action")],
        catalog.derive_for("xyne", "flow_action"),
    )
    return engine.extract(body, spec)


def test_the_person_is_the_xyne_user_who_answered() -> None:
    extracted = _extract(_body())
    assert extracted.handles == {"xyne_user_id": "cmh2x9k1p0003qz8w5f4r7t2e"}
    assert extracted.about == "customer"


def test_nothing_the_person_can_edit_names_anyone() -> None:
    """flowJSON is the browser's copy of the form: a phone, an email or a
    run id written into its data — by us or by the person — reaches no
    handle and no variable."""
    body = _body()
    body["context"]["flowJSON"]["data"].update(
        customer_mobile_number="+919900011122",
        email="someone@example.com",
        enrollment_id="00000000-0000-0000-0000-000000000101",
    )
    extracted = _extract(body)
    assert extracted.handles == {"xyne_user_id": "cmh2x9k1p0003qz8w5f4r7t2e"}
    assert not any("9900011122" in str(v) for v in extracted.variables.values())


def test_the_answer_reaches_a_plan_as_three_variables() -> None:
    variables = _extract(_body()).variables
    assert variables["reply"] == "form_submitted"
    assert variables["button"] == "submit-interview-availability"
    assert variables["flow_response"] == (
        "slots_2026-10-29: 10:00, 11:00\nslots_2026-10-30: 15:00"
    )


def test_any_form_reads_the_same_way() -> None:
    body = _body()
    body["values"] = {"city": "Pune", "pincode": "411001", "agree": True}
    assert _extract(body).variables["flow_response"] == (
        "city: Pune\npincode: 411001\nagree: true"
    )


def test_the_join_is_the_message_the_form_was_posted_in() -> None:
    assert flow_action.replied_to(_body()) == "cmh3b7r2t000aqz8m4n1x8k5p"
    fields = {f.path: f for f in catalog.CATALOG[("xyne", "flow_action")].fields}
    assert fields["replied_to"].keyable and not fields["replied_to"].variable


def test_a_letter_without_a_user_has_no_handle() -> None:
    """Quarantined `no_handle` by the worker, never guessed."""
    body = _body()
    body["context"].pop("userId")
    assert _extract(body).handles == {}


# --- the per-account bay's answers (a FAKE spec: the door names no provider) ---------


class _Bay:
    def __init__(self) -> None:
        self.merchant: Optional[str] = MERCHANT
        self.verify_error: Optional[Exception] = None
        self.letters: List[EventIn] = [_letter()]
        self.seen: List[Any] = []

    async def verify(self, bay: str, raw: bytes, headers: Any) -> Optional[str]:
        self.seen.append(("verify", bay))
        if self.verify_error is not None:
            raise self.verify_error
        return self.merchant

    async def envelope(
        self, bay: str, merchant_id: str, headers: Any, body: Dict
    ) -> List[EventIn]:
        self.seen.append(("envelope", bay, merchant_id))
        return self.letters


def _letter() -> EventIn:
    return EventIn(
        merchant_id=MERCHANT,
        source="fakebay",
        topic="t",
        external_id="m1:abc",
        payload={"a": 1},
    )


STORED = {"type": "stored"}
EMPTY = {"type": "nothing"}
FAILED = {"type": "failed"}


@pytest.fixture
def bay(monkeypatch: pytest.MonkeyPatch) -> _Bay:
    fake = _Bay()
    monkeypatch.setitem(
        ingress.BAY_INGRESS,
        "fakebay",
        ingress.BayIngressSpec(
            verify=fake.verify,
            envelope=fake.envelope,
            stored_reply=STORED,
            empty_reply=EMPTY,
            failed_reply=FAILED,
        ),
    )
    return fake


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []

    async def _ingest(**kwargs: Any) -> str:
        kept.append(kwargs)
        return "event-1"

    monkeypatch.setattr(record_api, "ingest_event", _ingest)
    return kept


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(record_api.webhook_router, prefix="/ingest/webhooks")
    return TestClient(app)


def test_a_verified_callback_is_filed_under_the_merchant_verify_answered(
    client: TestClient, bay: _Bay, stored: List[Dict[str, Any]]
) -> None:
    response = client.post("/ingest/webhooks/fakebay/app_1", content=b'{"x": 1}')
    assert (response.status_code, response.json()) == (200, STORED)
    assert bay.seen == [("verify", "app_1"), ("envelope", "app_1", MERCHANT)]
    assert [s["merchant_id"] for s in stored] == [MERCHANT]


def test_an_unknown_provider_is_404(client: TestClient, bay: _Bay) -> None:
    assert (
        client.post("/ingest/webhooks/nobody/app_1", content=b"{}").status_code == 404
    )


def test_a_bay_nobody_holds_or_a_bad_signature_is_403(
    client: TestClient, bay: _Bay, stored: List[Dict[str, Any]]
) -> None:
    bay.merchant = None
    response = client.post("/ingest/webhooks/fakebay/app_1", content=b"{}")
    assert response.status_code == 403 and response.content == b""
    assert stored == []


def test_a_verify_that_cannot_read_is_503_with_the_failure_body(
    client: TestClient, bay: _Bay, stored: List[Dict[str, Any]]
) -> None:
    bay.verify_error = RuntimeError("pool down")
    response = client.post("/ingest/webhooks/fakebay/app_1", content=b"{}")
    assert (response.status_code, response.json()) == (503, FAILED)
    assert stored == []


def test_a_store_failure_is_503_with_the_failure_body(
    client: TestClient, bay: _Bay, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _down(**kwargs: Any) -> str:
        raise RuntimeError("db down")

    monkeypatch.setattr(record_api, "ingest_event", _down)
    response = client.post("/ingest/webhooks/fakebay/app_1", content=b"{}")
    assert (response.status_code, response.json()) == (503, FAILED)


def test_a_callback_with_nothing_to_file_is_acknowledged(
    client: TestClient, bay: _Bay, stored: List[Dict[str, Any]]
) -> None:
    bay.letters = []
    response = client.post("/ingest/webhooks/fakebay/app_1", content=b"{}")
    assert (response.status_code, response.json()) == (200, EMPTY)
    assert stored == []


def test_a_signed_body_that_is_not_an_object_is_400(
    client: TestClient, bay: _Bay, stored: List[Dict[str, Any]]
) -> None:
    assert (
        client.post("/ingest/webhooks/fakebay/app_1", content=b"[1]").status_code == 400
    )
    assert stored == []


def test_an_oversized_body_is_413(
    client: TestClient, bay: _Bay, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(record_api, "MAX_LETTER_BYTES", 8)
    response = client.post(
        "/ingest/webhooks/fakebay/app_1", content=b'{"x": 123456789}'
    )
    assert response.status_code == 413
    assert bay.seen == []  # refused before verify


def test_an_envelope_that_fails_is_503_with_the_failure_body(
    client: TestClient, bay: _Bay, stored: List[Dict[str, Any]]
) -> None:
    async def _broken(*args: Any) -> List[EventIn]:
        raise RuntimeError("bad body walk")

    bay.envelope = _broken  # type: ignore[method-assign]
    spec = ingress.BAY_INGRESS["fakebay"]
    ingress.BAY_INGRESS["fakebay"] = spec._replace(envelope=_broken)
    response = client.post("/ingest/webhooks/fakebay/app_1", content=b"{}")
    assert (response.status_code, response.json()) == (503, FAILED)
    assert stored == []


def test_a_submit_xyne_did_not_shape_as_documented_is_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Xyne never retries, so dropping it silently would lose the answer
    without a trace: the warning names the app, never the body."""
    said: List[str] = []
    monkeypatch.setattr(inbound.logger, "warning", lambda message: said.append(message))
    body = _body()
    body["context"].pop("messageId")
    assert inbound.letters(body, FLOW_ACTION, APP_ID) == []
    assert len(said) == 1 and APP_ID in said[0] and "10:00" not in said[0]
    edit = _body()
    edit["type"] = "inputChange"
    inbound.letters(edit, FLOW_ACTION, APP_ID)
    assert len(said) == 1  # a field edit is expected, not a warning


def test_a_bay_without_replies_answers_like_the_shared_bay(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, stored: List[Dict[str, Any]]
) -> None:
    fake = _Bay()
    monkeypatch.setitem(
        ingress.BAY_INGRESS,
        "plainbay",
        ingress.BayIngressSpec(verify=fake.verify, envelope=fake.envelope),
    )
    response = client.post("/ingest/webhooks/plainbay/app_1", content=b"{}")
    assert (response.status_code, response.content) == (200, b"")


# --- the Xyne bay's verify: the app in the URL -> its connection -> its secret --------


def _installation(status: str = "healthy") -> ConnectorInstallation:
    return ConnectorInstallation(
        id="inst-1",
        merchant_id=MERCHANT,
        connector_key="xyne",
        external_account_id=APP_ID,
        credential_id="cred-1",
        status=status,
    )


@pytest.fixture
def xyne_app(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "installation": _installation(),
        "bundle": CredentialBundle(values={"webhook_secret": SECRET}),
        "asked": [],
    }

    async def _for_inbound(connector_key: str, account: str) -> Any:
        state["asked"].append((connector_key, account))
        return state["installation"] if account == APP_ID else None

    async def _bundle(installation: ConnectorInstallation) -> CredentialBundle:
        if isinstance(state["bundle"], Exception):
            raise state["bundle"]
        return state["bundle"]

    monkeypatch.setattr(
        connectivity_ingress.installation_accessor,
        "get_installation_for_inbound",
        _for_inbound,
    )
    monkeypatch.setattr(connectivity_ingress.installations, "bundle_for", _bundle)
    return state


async def test_the_apps_owner_is_the_merchant(xyne_app: Dict[str, Any]) -> None:
    raw = _raw(_body())
    headers = {"X-Xyne-Signature": _sign(raw), **FLOW_ACTION}
    assert await connectivity_ingress._xyne_verify(APP_ID, raw, headers) == MERCHANT
    assert xyne_app["asked"] == [("xyne", APP_ID)]


async def test_another_apps_secret_does_not_verify(xyne_app: Dict[str, Any]) -> None:
    raw = _raw(_body())
    headers = {"X-Xyne-Signature": _sign(raw, "app_other-secret")}
    assert await connectivity_ingress._xyne_verify(APP_ID, raw, headers) is None


async def test_an_app_nobody_connected_is_refused(xyne_app: Dict[str, Any]) -> None:
    raw = _raw(_body())
    headers = {"X-Xyne-Signature": _sign(raw)}
    assert await connectivity_ingress._xyne_verify("app_unknown", raw, headers) is None


@pytest.mark.parametrize("status", ["revoked", "disabled", "degraded"])
async def test_an_unusable_connection_is_refused(
    xyne_app: Dict[str, Any], status: str
) -> None:
    xyne_app["installation"] = _installation(status)
    raw = _raw(_body())
    headers = {"X-Xyne-Signature": _sign(raw)}
    assert await connectivity_ingress._xyne_verify(APP_ID, raw, headers) is None


@pytest.mark.parametrize(
    "bundle",
    [installations.AccountError("credential missing"), CredentialBundle(values={})],
)
async def test_a_connection_without_a_usable_secret_is_refused(
    xyne_app: Dict[str, Any], bundle: Any
) -> None:
    xyne_app["bundle"] = bundle
    raw = _raw(_body())
    headers = {"X-Xyne-Signature": _sign(raw)}
    assert await connectivity_ingress._xyne_verify(APP_ID, raw, headers) is None


async def test_the_envelope_files_under_the_merchant_verify_proved() -> None:
    [letter] = await connectivity_ingress._xyne_envelope(
        APP_ID, MERCHANT, FLOW_ACTION, _body()
    )
    assert (letter.merchant_id, letter.source, letter.topic) == (
        MERCHANT,
        "xyne",
        "flow_action",
    )
    assert letter.schema_version == "2.0"


def test_xyne_answers_in_its_own_words() -> None:
    """Xyne renders these (packages/shared validation: appActionResponseSchema):
    close_screen on a stored answer, ack for what we do not file, and a
    typed error — shown to the person as sent — on a failure."""
    spec = connectivity_ingress.XYNE_INGRESS
    assert spec.stored_reply == {
        "type": "close_screen",
        "finalMessage": "Response received",
    }
    assert spec.empty_reply == {"type": "ack"}
    assert spec.failed_reply is not None and spec.failed_reply["type"] == "error"
    assert spec.failed_reply["retryable"] is True


def test_the_bay_is_registered_at_the_composition_root() -> None:
    import app.crm.api  # noqa: F401  (the root registers on import)

    assert ingress.BAY_INGRESS["xyne"] is connectivity_ingress.XYNE_INGRESS


def test_the_example_plan_listens_for_a_xyne_answer() -> None:
    plan = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs/crm/plans/xyne-interview-availability.json"
        ).read_text()
    )
    listen = next(n for n in plan["nodes"] if n["id"] == "await-availability")
    assert listen["topics"] == ["flow_action"] and listen["key"] == "reply"
    assert ["await-availability", "confirm-slots", "form_submitted"] in plan["edges"]

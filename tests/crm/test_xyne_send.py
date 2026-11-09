"""Xyne 4/5 — sending a registered form to a person's Xyne user id.

Mock only: Xyne's HTTP is doubles.stub_http on the module that makes the
call, and every database read is a monkeypatched accessor or contract. What
a real Postgres adds (087's CHECKs, the live-handle and letter reads) is
pinned in test_xyne_send_integration.

The laws, in the order a send meets them:
  - the form is ours: registered with us, checked by Xyne on submit by
    being sent to the app's preview user, approved at once;
  - the send is addressed by the CHANNEL: a Xyne send goes to the customer's
    live Xyne user id, a WhatsApp send to the run's phone as before;
  - a declared list rides as the list itself, read from the run's letter,
    only on a channel that takes structured variables;
  - the gate can check a Xyne id, and refuses what it cannot check;
  - Xyne's answer is classified, and its messageId is kept — the id the
    answer (3/5) carries back.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, cast
from uuid import uuid4

import httpx
import pytest

import app.crm.connectivity.messaging.shared.gate as gate_module
import app.crm.outreach.nodes.send as send_node
from app.crm.connectivity.channels.registry import (
    CHANNELS,
    gate_handle_kind_for,
    registers_templates_for,
    takes_structured_variables,
)
from app.crm.connectivity.connectors import CONNECTORS, connector_for_channel
from app.crm.connectivity.messaging.outbound.queue import normalize_address
from app.crm.connectivity.providers.xyne import (
    adapter as adapter_module,
    client as xyne_client,
    form,
    onboard,
    templates as templates_module,
)
from app.crm.connectivity.providers.xyne.adapter import XyneAdapter, classify
from app.crm.connectivity.providers.xyne.templates import (
    XyneTemplateError,
    XyneTemplates,
    template_id,
)
from app.crm.connectivity.schemas.connector import ChannelBinding, ConnectorInstallation
from app.crm.connectivity.schemas.message import (
    CredentialBundle,
    QueuedMessage,
    SendRoute,
)
from app.crm.connectivity.schemas.template import ApprovedTemplate, TemplateDraft
from app.crm.connectivity.vocabulary.reasons import (
    REASON_BAD_ADDRESS,
    REASON_BAD_VARIABLES,
    REASON_CLASS_MERCHANT,
    REASON_NO_CREDENTIAL,
    REASON_NO_TEMPLATE,
    REASON_XYNE_FORBIDDEN,
    REASON_XYNE_FORM_REFUSED,
    REASON_XYNE_MISSING_PERMISSION,
    REASON_XYNE_RECIPIENT_UNKNOWN,
    REASON_XYNE_TOKEN_REFUSED,
    reason_class,
)
from app.crm.connectivity.vocabulary.status import TEMPLATE_APPROVED
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.schemas import EnrollmentRun, WorkflowNode
from app.crm.platform import suppression
from app.crm.record import events as record_events
from app.crm.record.contracts import AmbiguousTopic
from app.crm.record.schemas import CatalogField, RawEvent
from tests.crm.doubles import stub_http

BASE = "https://spaces.xyne.juspay.net"
POST_URL = f"{BASE}/api/apps/chat/postMessage"
#: No long digit run in it: the excerpt masks those, which would hide a
#: leak of a token that had one.
TOKEN = "xyne-app-token-abcdefghij"
APP_ID = "app_hire01"
ASHA = "cm8xasha0001"
PREVIEWER = "cm8xhrlead01"

#: A form as Pragathi's developers would write it for Xyne: a heading with a
#: blank, a multiselect whose options are the person's free slots, Submit.
LAYOUT: List[Dict[str, Any]] = [
    {"id": "h", "type": "heading", "props": {"content": "Hi {candidate_name}"}},
    {
        "id": "slots",
        "type": "multiselect",
        "props": {"name": "slots", "label": "Free slots", "options": "$slots"},
    },
    {
        "id": "go",
        "type": "button",
        "props": {
            "label": "Submit",
            "action": {"type": "submit", "actionId": "submit"},
        },
    },
]
SLOTS = [{"label": "Mon 10:00", "value": "s1"}, {"label": "Mon 14:00", "value": "s2"}]


def _bundle(**overrides: Any) -> CredentialBundle:
    values: Dict[str, Any] = {
        onboard.API_TOKEN: TOKEN,
        onboard.WEBHOOK_SECRET: "a-webhook-secret-of-length",
        onboard.BASE_URL: BASE,
        onboard.PREVIEW_USER_ID: PREVIEWER,
    }
    values.update(overrides)
    return CredentialBundle(values={k: v for k, v in values.items() if v is not None})


def _route(**overrides: Any) -> SendRoute:
    fields: Dict[str, Any] = dict(
        installation=ConnectorInstallation(
            id="i-1",
            merchant_id="pragathi",
            connector_key="xyne",
            external_account_id=APP_ID,
            credential_id="cred-1",
            status="healthy",
        ),
        binding=ChannelBinding(
            id="b-1",
            merchant_id="pragathi",
            channel="xyne",
            installation_id="i-1",
            address=APP_ID,
            capabilities={},
            is_primary=True,
            status="active",
        ),
        bundle=_bundle(),
        template=ApprovedTemplate(
            id="t-1",
            name="interview_slots",
            language="en",
            components=LAYOUT,
        ),
    )
    fields.update(overrides)
    return SendRoute(**fields)


def _message(**overrides: Any) -> QueuedMessage:
    fields: Dict[str, Any] = dict(
        id="msg-1",
        merchant_id="pragathi",
        customer_id="c-asha",
        channel="xyne",
        sent_to_address=ASHA,
        source_kind="workflow",
        purpose_key="utility.interview.availability",
        template_id="interview_slots",
        variables={"candidate_name": "Asha", "slots": SLOTS},
        dedupe_key="run-1:ask",
        attempt=1,
        next_attempt_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return QueuedMessage(**fields)


def _created(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "eventType": "message.posted",
            "conversationId": "cv-1",
            "messageId": "xm-1",
        },
    )


def _body(request: httpx.Request) -> Dict[str, Any]:
    return json.loads(request.content)


# --- the registry: Xyne is a channel, addressed by a row kind --------------------


def test_xyne_is_a_channel_addressed_by_the_xyne_user_id() -> None:
    assert gate_handle_kind_for("xyne") == "xyne_user_id"
    assert registers_templates_for("xyne") is True
    assert takes_structured_variables("xyne") is True


def test_only_xyne_takes_structured_variables() -> None:
    """Every other channel keeps the one-line-of-text posture — WhatsApp's
    blanks carry no list, and an unknown channel answers no."""
    assert [name for name, ch in CHANNELS.items() if ch.structured_variables] == [
        "xyne"
    ]
    assert takes_structured_variables("whatsapp") is False
    assert takes_structured_variables("telegram") is False


def test_the_connector_and_the_channel_name_one_spec() -> None:
    assert connector_for_channel("xyne") is CONNECTORS["xyne"]


def test_a_xyne_address_is_normalized_like_resolve_stores_it() -> None:
    assert normalize_address("xyne", f"  {ASHA} ") == ASHA
    assert normalize_address("xyne", "env-api-user") is None
    assert normalize_address("xyne", "not an id") is None
    # And the phone channel is untouched by the row-kind branch.
    assert normalize_address("whatsapp", "9812345678") == "+919812345678"


# --- the form: layout checks and the flow one person is sent -------------------


def test_a_layout_needs_components_each_with_an_id_and_a_type() -> None:
    assert form.layout_problem(LAYOUT) is None
    assert form.layout_problem([]) is not None
    assert form.layout_problem({"components": LAYOUT}) is not None
    assert form.layout_problem(["text"]) is not None
    assert "'id'" in (form.layout_problem([{"type": "text"}]) or "")
    assert "'type'" in (form.layout_problem([{"id": "x", "type": " "}]) or "")


def test_a_layout_over_the_bound_is_refused_before_xyne_is_asked() -> None:
    big = [
        {"id": "t", "type": "text", "props": {"content": "x" * form.MAX_LAYOUT_BYTES}}
    ]
    assert "KB" in (form.layout_problem(big) or "")


def test_the_flow_fills_blanks_and_carries_every_variable_in_data() -> None:
    flow = form.build_flow(LAYOUT, {"candidate_name": "Asha", "slots": SLOTS}, "s1")
    assert isinstance(flow, dict)
    assert flow["version"] == "2.0" and flow["screenId"] == "s1"
    assert flow["components"][0]["props"]["content"] == "Hi Asha"
    # "$slots" is Xyne's own reference — left as written, filled from data.
    assert flow["components"][1]["props"]["options"] == "$slots"
    assert flow["data"] == {"candidate_name": "Asha", "slots": SLOTS}
    # The state Xyne's schema requires, empty.
    assert flow["state"] == form.empty_state()
    assert set(flow["state"]) >= {
        "values",
        "touched",
        "errors",
        "submitting",
        "submitted",
        "history",
    }
    # The layout itself is never mutated.
    assert LAYOUT[0]["props"]["content"] == "Hi {candidate_name}"


def test_a_blank_with_no_value_is_refused_never_shown() -> None:
    """A person must never read "{candidate_name}"."""
    refused = form.build_flow(LAYOUT, {"slots": SLOTS}, "s1")
    assert refused == "no value for {candidate_name}"
    # A list is not text: it cannot fill a blank.
    assert isinstance(form.build_flow(LAYOUT, {"candidate_name": SLOTS}, "s1"), str)


def test_a_preview_shows_the_layout_as_written() -> None:
    flow = form.build_flow(LAYOUT, {}, "preview", preview=True)
    assert isinstance(flow, dict)
    assert flow["components"][0]["props"]["content"] == "Hi {candidate_name}"
    assert flow["data"] == {}


def test_a_value_is_filled_once_never_again() -> None:
    """One pass: a name that itself looks like a blank stays text."""
    layout = [{"id": "t", "type": "text", "props": {"content": "{a} and {b}"}}]
    flow = form.build_flow(layout, {"a": "{b}", "b": "B"}, "s")
    assert isinstance(flow, dict)
    assert flow["components"][0]["props"]["content"] == "{b} and B"


def test_braces_that_are_not_a_blank_stay_text() -> None:
    layout = [{"id": "t", "type": "text", "props": {"content": "{ not } {a-b}"}}]
    flow = form.build_flow(layout, {}, "s")
    assert isinstance(flow, dict)
    assert flow["components"][0]["props"]["content"] == "{ not } {a-b}"


def test_a_numbered_blank_is_a_blank() -> None:
    """The WhatsApp habit ({"1": fact}) must not reach a person as "{1}"."""
    layout = [{"id": "t", "type": "text", "props": {"content": "Hi {1}"}}]
    flow = form.build_flow(layout, {"1": "Asha"}, "s")
    assert isinstance(flow, dict)
    assert flow["components"][0]["props"]["content"] == "Hi Asha"
    assert form.build_flow(layout, {}, "s") == "no value for {1}"


@pytest.mark.parametrize(
    "slots",
    [
        "Mon 10:00, Mon 14:00",  # the run's line of text
        [],  # nothing to pick
        ["Mon 10:00", "Mon 14:00"],  # plain strings
        [{"label": "Mon 10:00"}],  # no value
        [{"label": "", "value": "s1"}],  # empty label
        [{"label": "Mon", "value": 1}],  # a value that is not text
        None,
    ],
)
def test_a_picker_gets_a_list_of_label_and_value_or_nothing_is_sent(slots) -> None:
    """Xyne accepts any data and shows an EMPTY picker for these — the
    person would get a form they cannot answer, recorded as accepted."""
    variables: Dict[str, Any] = {"candidate_name": "Asha"}
    if slots is not None:
        variables["slots"] = slots
    refused = form.build_flow(LAYOUT, variables, "s")
    assert isinstance(refused, str) and "data.slots" in refused


def test_a_picker_nested_in_a_card_is_checked_too() -> None:
    layout = [
        {
            "id": "card",
            "type": "card",
            "children": [
                {
                    "id": "p",
                    "type": "select",
                    "props": {"name": "d", "options": "$days"},
                }
            ],
        }
    ]
    assert form.option_refs(layout) == {"days"}
    assert isinstance(form.build_flow(layout, {"days": "Mon"}, "s"), str)
    assert isinstance(form.build_flow(layout, {"days": SLOTS}, "s"), dict)


def test_a_preview_does_not_need_the_lists() -> None:
    assert isinstance(form.build_flow(LAYOUT, {}, "preview", preview=True), dict)


def test_a_flow_over_the_bound_is_refused() -> None:
    huge = [{"label": "x" * 1000, "value": str(i)} for i in range(80)]
    assert "KB" in str(
        form.build_flow(LAYOUT, {"candidate_name": "A", "slots": huge}, "s")
    )


# --- the send face -----------------------------------------------------------


async def test_a_form_is_posted_to_the_persons_xyne_id(monkeypatch) -> None:
    seen = stub_http(monkeypatch, adapter_module, _created)
    outcome = await XyneAdapter().deliver(_message(), _route())

    assert outcome.status == "accepted"
    # The id the answer's context.messageId will carry (3/5's replied_to).
    assert outcome.provider_message_id == "xm-1"
    (call,) = seen
    assert call.method == "POST" and str(call.url) == POST_URL
    assert call.headers["authorization"] == f"Bearer {TOKEN}"
    body = _body(call)
    assert body["channelId"] == ASHA
    assert body["flow"]["components"][0]["props"]["content"] == "Hi Asha"
    assert body["flow"]["data"]["slots"] == SLOTS
    assert body["flow"]["screenId"] == "interview_slots"


async def test_a_redirect_is_never_followed(monkeypatch) -> None:
    """The token must not ride a 30x somewhere else."""
    seen = stub_http(
        monkeypatch,
        adapter_module,
        lambda r: httpx.Response(302, headers={"location": "https://evil.example.com"}),
    )
    outcome = await XyneAdapter().deliver(_message(), _route())
    assert len(seen) == 1
    assert outcome.status == "failed" and outcome.retryable is True


@pytest.mark.parametrize(
    "route_change, message_change, reason",
    [
        ({"bundle": _bundle(**{onboard.API_TOKEN: None})}, {}, REASON_NO_CREDENTIAL),
        (
            {"bundle": _bundle(**{onboard.BASE_URL: "https://evil.example.com"})},
            {},
            REASON_NO_CREDENTIAL,
        ),
        ({"template": None}, {}, REASON_NO_TEMPLATE),
        ({}, {"sent_to_address": "env-api-user"}, REASON_BAD_ADDRESS),
        ({}, {"variables": {"slots": SLOTS}}, REASON_BAD_VARIABLES),
        (
            {},
            {"variables": {"candidate_name": "Asha", "slots": "Mon 10:00"}},
            REASON_BAD_VARIABLES,
        ),
    ],
)
async def test_what_cannot_be_sent_is_blocked_before_the_wire(
    monkeypatch, route_change, message_change, reason
) -> None:
    seen = stub_http(monkeypatch, adapter_module, _created)
    outcome = await XyneAdapter().deliver(
        _message(**message_change), _route(**route_change)
    )
    assert (outcome.status, outcome.reason, outcome.retryable) == (
        "blocked",
        reason,
        False,
    )
    assert seen == []


async def test_no_answer_is_a_retry(monkeypatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    stub_http(monkeypatch, adapter_module, boom)
    outcome = await XyneAdapter().deliver(_message(), _route())
    assert outcome.status == "failed" and outcome.retryable is True


async def test_accepted_without_a_message_id_is_still_accepted(monkeypatch) -> None:
    stub_http(monkeypatch, adapter_module, lambda r: httpx.Response(201, json={}))
    outcome = await XyneAdapter().deliver(_message(), _route())
    assert outcome.status == "accepted" and outcome.provider_message_id is None


@pytest.mark.parametrize(
    "code, body, reason, retryable",
    [
        (
            400,
            {"error": "Invalid flowJSON", "code": "VALIDATION_ERROR"},
            REASON_XYNE_FORM_REFUSED,
            False,
        ),
        (401, {"error": "Unauthorized"}, REASON_XYNE_TOKEN_REFUSED, False),
        (
            403,
            {"error": "missing_permission", "required": "chat:write"},
            REASON_XYNE_MISSING_PERMISSION,
            False,
        ),
        (
            403,
            {"error": "Forbidden", "message": "Bot does not have channel access"},
            REASON_XYNE_FORBIDDEN,
            False,
        ),
        (
            404,
            {"error": "Not Found", "message": "Channel not found"},
            REASON_XYNE_RECIPIENT_UNKNOWN,
            False,
        ),
        (409, {}, "http_409", False),
        (429, {}, "http_429", True),
        (500, {"error": "Internal server error"}, "http_500", True),
        (503, "<html>down</html>", "http_503", True),
    ],
)
async def test_xynes_answer_is_classified(
    monkeypatch, code, body, reason, retryable
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(code, text=body)
        return httpx.Response(code, json=body)

    stub_http(monkeypatch, adapter_module, answer)
    outcome = await XyneAdapter().deliver(_message(), _route())
    assert (outcome.status, outcome.reason, outcome.retryable) == (
        "failed",
        reason,
        retryable,
    )


def test_every_xyne_refusal_is_the_merchants_to_fix() -> None:
    for reason in (
        REASON_XYNE_FORM_REFUSED,
        REASON_XYNE_TOKEN_REFUSED,
        REASON_XYNE_MISSING_PERMISSION,
        REASON_XYNE_FORBIDDEN,
        REASON_XYNE_RECIPIENT_UNKNOWN,
    ):
        assert reason_class(reason) == REASON_CLASS_MERCHANT, reason


def test_classify_is_pure() -> None:
    assert classify(401).reason == REASON_XYNE_TOKEN_REFUSED
    assert classify(403, "missing_permission").reason == REASON_XYNE_MISSING_PERMISSION
    assert classify(301).retryable is True


async def test_the_token_never_reaches_a_log_line(monkeypatch) -> None:
    lines: List[str] = []
    monkeypatch.setattr(
        adapter_module.logger, "warning", lambda text, *a, **k: lines.append(text)
    )
    stub_http(
        monkeypatch,
        adapter_module,
        lambda r: httpx.Response(
            400, json={"error": f"bad token {TOKEN} for 9812345678"}
        ),
    )
    await XyneAdapter().deliver(_message(), _route())
    assert lines and all(
        TOKEN not in line and "9812345678" not in line for line in lines
    )


# --- the template face: approved by Xyne being sent the preview ---------------


@pytest.fixture(autouse=True)
def _preview_user_not_stopped(monkeypatch) -> None:
    """The template face asks the STOP book about the preview user; these
    tests run without a database, so the book says "not stopped" unless a
    test says otherwise."""

    async def is_suppressed(handles: Dict[str, str]) -> bool:
        return False

    monkeypatch.setattr(templates_module, "is_suppressed", is_suppressed)


def _draft(**overrides: Any) -> TemplateDraft:
    fields: Dict[str, Any] = dict(
        name="interview_slots", language="en", category="UTILITY", components=LAYOUT
    )
    fields.update(overrides)
    return TemplateDraft(**fields)


async def test_submit_sends_the_preview_and_approves(monkeypatch) -> None:
    seen = stub_http(monkeypatch, xyne_client, _created)
    state = await XyneTemplates().submit(_bundle(), APP_ID, _draft())

    assert state.status == TEMPLATE_APPROVED
    assert (state.provider_template_id or "").startswith("xyne:")
    again = await XyneTemplates().submit(_bundle(), APP_ID, _draft())
    # Fresh every time: 061's unique spans every row and status, so an id
    # built from the app and the name would collide once an app changes
    # hands and the new holder registers the same name.
    assert again.provider_template_id != state.provider_template_id
    assert state.name == "interview_slots" and state.category == "UTILITY"
    call = seen[0]
    assert len(seen) == 2  # one preview per submit
    assert str(call.url) == POST_URL
    body = _body(call)
    # To the preview user, the layout as written, no person's data.
    assert body["channelId"] == PREVIEWER
    assert body["flow"]["components"][0]["props"]["content"] == "Hi {candidate_name}"
    assert body["flow"]["data"] == {}


async def test_xynes_refusal_reaches_the_merchant_in_xynes_words(monkeypatch) -> None:
    stub_http(
        monkeypatch,
        xyne_client,
        lambda r: httpx.Response(
            400,
            json={
                "error": "Invalid flowJSON",
                "code": "VALIDATION_ERROR",
                "details": ["components.1.props.label: Required"],
            },
        ),
    )
    with pytest.raises(XyneTemplateError) as refused:
        await XyneTemplates().submit(_bundle(), APP_ID, _draft())
    assert "Invalid flowJSON" in str(refused.value)
    assert "components.1.props.label: Required" in str(refused.value)


async def test_a_form_cannot_be_approved_without_a_preview_user(monkeypatch) -> None:
    seen = stub_http(monkeypatch, xyne_client, _created)
    with pytest.raises(XyneTemplateError, match="preview_user_id"):
        await XyneTemplates().submit(
            _bundle(**{onboard.PREVIEW_USER_ID: None}), APP_ID, _draft()
        )
    assert seen == []


async def test_a_preview_user_under_a_stop_gets_no_preview(monkeypatch) -> None:
    """The preview is a message to a person: a STOP on them (or a STOP book
    that cannot be read) means no preview, so the form is not approved."""
    asked: List[Dict[str, str]] = []

    async def is_suppressed(handles: Dict[str, str]) -> bool:
        asked.append(handles)
        return True

    monkeypatch.setattr(templates_module, "is_suppressed", is_suppressed)
    seen = stub_http(monkeypatch, xyne_client, _created)
    with pytest.raises(XyneTemplateError, match="asked not to be contacted"):
        await XyneTemplates().submit(_bundle(), APP_ID, _draft())
    assert seen == []
    assert asked == [{"xyne_user_id": PREVIEWER}]


async def test_a_broken_layout_never_reaches_xyne(monkeypatch) -> None:
    seen = stub_http(monkeypatch, xyne_client, _created)
    with pytest.raises(XyneTemplateError, match="component 0"):
        await XyneTemplates().submit(
            _bundle(), APP_ID, _draft(components=[{"type": "text"}])
        )
    assert seen == []


async def test_a_xyne_that_cannot_answer_is_a_try_again(monkeypatch) -> None:
    stub_http(
        monkeypatch, xyne_client, lambda r: httpx.Response(502, text="bad gateway")
    )
    with pytest.raises(XyneTemplateError, match="try again"):
        await XyneTemplates().submit(_bundle(), APP_ID, _draft())


async def test_an_edit_is_checked_the_same_way(monkeypatch) -> None:
    seen = stub_http(monkeypatch, xyne_client, _created)
    provider_id = template_id()
    state = await XyneTemplates().edit(_bundle(), APP_ID, provider_id, LAYOUT[:1])
    assert state.status == TEMPLATE_APPROVED
    assert state.provider_template_id == provider_id
    assert _body(seen[0])["flow"]["components"] == LAYOUT[:1]
    assert XyneTemplates.edits_in_place is True


async def test_retire_and_events_touch_nothing_at_xyne(monkeypatch) -> None:
    seen = stub_http(monkeypatch, xyne_client, _created)
    await XyneTemplates().retire(_bundle(), APP_ID, "x", "interview_slots", "en")
    assert XyneTemplates().normalize_event("template.status", {}) is None
    assert seen == []


def test_the_registry_id_is_fresh_and_ours() -> None:
    first, second = template_id(), template_id()
    assert first != second
    assert first.startswith("xyne:") and len(first) > len("xyne:")


def test_the_template_face_raises_the_ports_declared_type() -> None:
    """templates.py passes a TemplateProviderError through verbatim; anything
    else becomes a fixed sentence the merchant cannot act on."""
    from app.crm.connectivity.providers.base import TemplateProviderError

    assert issubclass(XyneTemplateError, TemplateProviderError)
    assert templates_module.XyneTemplateError is XyneTemplateError


# --- the gate can check a Xyne id, and refuses what it cannot check -----------


async def test_a_xyne_id_is_probed_in_its_stored_form(monkeypatch) -> None:
    probed: List[Any] = []

    async def probe(pairs: Any) -> Dict[str, bool]:
        probed.append(pairs)
        return {"suppressed": True}

    monkeypatch.setattr(suppression.accessor, "probe_suppression_pairs", probe)
    assert await suppression.is_suppressed({"xyne_user_id": f" {ASHA} "}) is True
    assert probed == [[("xyne_user_id", ASHA)]]


@pytest.mark.parametrize(
    "handles",
    [
        {"igsid": "1789"},  # a kind the book does not hold
        {"telegram_id": "t-1"},
        {"xyne_user_id": "env-api-user"},  # a value that will not normalize
        {"phone": "not a phone"},
        {"phone": ""},
    ],
)
async def test_the_gate_fails_closed_on_what_the_book_cannot_check(
    monkeypatch, handles
) -> None:
    async def probe(pairs: Any) -> Dict[str, bool]:
        raise AssertionError("an unchecked handle must not reach the probe")

    monkeypatch.setattr(suppression.accessor, "probe_suppression_pairs", probe)
    assert await suppression.is_suppressed(handles) is True


async def test_a_clean_phone_is_still_probed_and_allowed(monkeypatch) -> None:
    async def probe(pairs: Any) -> Dict[str, bool]:
        return {"suppressed": False}

    monkeypatch.setattr(suppression.accessor, "probe_suppression_pairs", probe)
    assert await suppression.is_suppressed({"phone": "9812345678"}) is False


def test_resolve_still_registers_phones_and_emails_only() -> None:
    """A Xyne id enters the book only through a STOP, never on sight."""
    assert set(suppression.HANDLE_KINDS) == {"phone", "email"}
    assert set(suppression.SUPPRESSIBLE_KINDS) == {"phone", "email", "xyne_user_id"}


def test_a_stop_on_a_xyne_id_is_recorded_in_its_stored_form() -> None:
    assert suppression._normalize_pair("xyne_user_id", f" {ASHA} ") == ASHA
    assert suppression._normalize_pair("xyne_user_id", "null") is None


# --- the send node: addressed by the channel, lists from the letter ------------


def _run(**context: Any) -> EnrollmentRun:
    now = datetime.now(timezone.utc)
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="pragathi",
        workflow_id=uuid4(),
        workflow_version=1,
        customer_id=uuid4(),
        status="running",
        current_node="ask",
        wake_at=None,
        entered_at=now,
        exited_at=None,
        exit_reason=None,
        context={"source_event_id": "e-1", **context},
        enrollment_key="k",
        attempts=1,
        last_error=None,
    )


DEFINITION = SimpleNamespace(purpose_key="utility.interview.availability")


def _xyne_node(**overrides: Any) -> WorkflowNode:
    fields: Dict[str, Any] = dict(
        id="ask",
        type="send",
        channel="xyne",
        template="interview_slots",
        variables={"candidate_name": "candidate_name", "slots": "slots"},
    )
    fields.update(overrides)
    return WorkflowNode(**fields)


def _wire(monkeypatch, *, live: Any = ASHA, lists: Any = None) -> SimpleNamespace:
    """The node's collaborators, faked; ``.queued`` holds the queue_message
    calls and ``.asked`` the identity reads."""
    queued: List[Dict[str, Any]] = []
    asked: List[Any] = []

    async def queue(**kwargs: Any) -> str:
        queued.append(kwargs)
        return "msg-1"

    async def no_blocks(run: Any, node: Any, definition: Any, names: Any) -> Any:
        return {}, None

    async def live_handle(merchant_id: str, customer_id: str, kind: str) -> Any:
        asked.append((merchant_id, customer_id, kind))
        return live

    async def declared(merchant_id: str, event_id: str, names: Any) -> Any:
        if isinstance(lists, Exception):
            raise lists
        return {} if lists is None else lists

    monkeypatch.setattr(send_node, "queue_message", queue)
    monkeypatch.setattr(send_node, "blocks_for", no_blocks)
    monkeypatch.setattr(send_node, "get_live_handle", live_handle)
    monkeypatch.setattr(send_node, "declared_lists", declared)
    return SimpleNamespace(queued=queued, asked=asked)


def _execute(run: EnrollmentRun, node: WorkflowNode) -> Dict[str, Any]:
    return asyncio.run(send_node.execute(run, node, cast(Any, DEFINITION)))


def test_a_xyne_send_goes_to_the_customers_live_xyne_id(monkeypatch) -> None:
    wired = _wire(monkeypatch, lists={"slots": SLOTS})
    run = _run(
        candidate_name="Asha", slots="Mon 10:00, Mon 14:00", phone="+919812345678"
    )
    _execute(run, _xyne_node())

    (call,) = wired.queued
    assert call["address"] == ASHA  # never the phone on the run
    assert call["channel"] == "xyne"
    assert wired.asked == [("pragathi", str(run.customer_id), "xyne_user_id")]
    # The list itself, not the line of text the run holds.
    assert call["variables"] == {"candidate_name": "Asha", "slots": SLOTS}


def test_a_customer_with_no_xyne_id_parks_by_name(monkeypatch) -> None:
    wired = _wire(monkeypatch, live=None)
    with pytest.raises(NodeParked, match="no xyne_user_id"):
        _execute(_run(candidate_name="Asha", phone="+919812345678"), _xyne_node())
    assert wired.queued == []


def test_a_whatsapp_send_is_unchanged(monkeypatch) -> None:
    """The phone from the run, text variables only, no identity read and
    no letter read — exactly as before channels could say otherwise."""
    wired = _wire(monkeypatch, lists={"slots": SLOTS})
    node = WorkflowNode(
        id="nudge",
        type="send",
        channel="whatsapp",
        template="cod_confirm_1",
        variables={"1": "candidate_name", "2": "slots"},
    )
    _execute(
        _run(candidate_name="Asha", slots="Mon 10:00", phone="+919812345678"), node
    )
    (call,) = wired.queued
    assert call["address"] == "+919812345678"
    assert call["variables"] == {"1": "Asha", "2": "Mon 10:00"}
    assert wired.asked == []


def test_a_whatsapp_send_with_no_phone_parks_as_before(monkeypatch) -> None:
    _wire(monkeypatch)
    node = WorkflowNode(id="n", type="send", channel="whatsapp", template="t")
    with pytest.raises(NodeParked, match="no phone in run context"):
        _execute(_run(), node)


def test_a_declared_list_the_letter_does_not_carry_parks(monkeypatch) -> None:
    """Never the run's line of text in its place: that posts a form whose
    picker is empty and records it accepted."""
    wired = _wire(monkeypatch, lists={"slots": None})
    with pytest.raises(NodeParked, match="carries no slots list"):
        _execute(_run(candidate_name="Asha", slots="Mon 10:00"), _xyne_node())
    assert wired.queued == []


def test_a_fact_that_is_no_declared_list_still_comes_from_the_run(monkeypatch) -> None:
    """Only declared lists are read from the letter; anything else is the
    run's own text fact, and a missing one still parks by name."""
    wired = _wire(monkeypatch, lists={})
    _execute(_run(candidate_name="Asha", slots="Mon 10:00"), _xyne_node())
    assert wired.queued[0]["variables"] == {
        "candidate_name": "Asha",
        "slots": "Mon 10:00",
    }

    _wire(monkeypatch, lists={})
    with pytest.raises(NodeParked, match="slots"):
        _execute(_run(candidate_name="Asha"), _xyne_node())


def test_too_many_items_park_rather_than_truncate(monkeypatch) -> None:
    many = [
        {"label": str(i), "value": str(i)}
        for i in range(send_node.STRUCTURED_MAX_ITEMS + 1)
    ]
    wired = _wire(monkeypatch, lists={"slots": many})
    with pytest.raises(NodeParked, match="at most"):
        _execute(_run(candidate_name="Asha"), _xyne_node())
    assert wired.queued == []


def test_too_many_bytes_park_rather_than_truncate(monkeypatch) -> None:
    wide = [{"label": "x" * 2000, "value": str(i)} for i in range(10)]
    _wire(monkeypatch, lists={"slots": wide})
    with pytest.raises(NodeParked, match="bytes"):
        _execute(_run(candidate_name="Asha"), _xyne_node())


def test_an_ambiguous_letter_parks(monkeypatch) -> None:
    _wire(monkeypatch, lists=AmbiguousTopic("interview.requested", ["a", "b"]))
    with pytest.raises(NodeParked):
        _execute(_run(candidate_name="Asha"), _xyne_node())


def test_no_letter_pointer_reads_no_letter(monkeypatch) -> None:
    wired = _wire(monkeypatch, lists={"slots": SLOTS})
    run = _run(candidate_name="Asha", slots="Mon 10:00")
    run.context.pop("source_event_id")
    _execute(run, _xyne_node())
    assert wired.queued[0]["variables"]["slots"] == "Mon 10:00"


# --- record: the declared list, read from the letter ---------------------------


def _letter(payload: Dict[str, Any], topic: str = "interview.requested") -> RawEvent:
    return RawEvent(
        id="e-1",
        merchant_id="pragathi",
        source="pragathi",
        topic=topic,
        schema_version="1",
        external_id="x-1",
        payload=payload,
        received_at=datetime.now(timezone.utc),
        occurred_at=None,
        customer_id=None,
        attempts=1,
    )


FIELDS = {
    "payload.slots": CatalogField(
        path="payload.slots", type="list", label="Slots", variable=True
    ),
    "payload.tags": CatalogField(path="payload.tags", type="list", label="Tags"),
    "payload.old": CatalogField(
        path="payload.old", type="list", label="Old", variable=True, deprecated=True
    ),
    "payload.candidate_name": CatalogField(
        path="payload.candidate_name", type="text", label="Name", variable=True
    ),
}


def _record(monkeypatch, letter: Any, fields: Any = FIELDS) -> List[Any]:
    asked: List[Any] = []

    async def get_event(merchant_id: str, event_id: str) -> Any:
        asked.append((merchant_id, event_id))
        return letter

    async def catalog(merchant_id: str, topic: str) -> Any:
        return fields

    monkeypatch.setattr(record_events.accessor, "get_event", get_event)
    monkeypatch.setattr(record_events, "catalog_fields", catalog)
    return asked


async def test_a_declared_list_is_read_whole_from_the_letter(monkeypatch) -> None:
    asked = _record(
        monkeypatch,
        _letter(
            {"slots": SLOTS, "tags": ["a"], "old": ["x"], "candidate_name": "Asha"}
        ),
    )
    found = await record_events.declared_lists(
        "pragathi", "e-1", {"slots", "tags", "old", "candidate_name"}
    )
    # Only a declared, live, LIST variable: not a list that is no variable,
    # not a deprecated one, not text.
    assert found == {"slots": SLOTS}
    assert asked == [("pragathi", "e-1")]


async def test_nothing_asked_reads_nothing(monkeypatch) -> None:
    asked = _record(monkeypatch, _letter({"slots": SLOTS}))
    assert await record_events.declared_lists("pragathi", "e-1", set()) == {}
    assert asked == []


async def test_a_gone_letter_or_an_unknown_topic_reads_empty(monkeypatch) -> None:
    _record(monkeypatch, None)
    assert await record_events.declared_lists("pragathi", "e-1", {"slots"}) == {}
    _record(monkeypatch, _letter({"slots": SLOTS}), fields=None)
    assert await record_events.declared_lists("pragathi", "e-1", {"slots"}) == {}


async def test_a_letter_without_a_declared_list_says_so(monkeypatch) -> None:
    """None, not absence: the caller must tell "a list field with nothing in
    it" from "not a list field" (which falls back to the run's text)."""
    _record(monkeypatch, _letter({"candidate_name": "Asha"}))
    assert await record_events.declared_lists("pragathi", "e-1", {"slots"}) == {
        "slots": None
    }


async def test_the_item_filter_applies_as_it_does_to_the_text(monkeypatch) -> None:
    from app.crm.shared.predicate import Condition

    fields = {
        "payload.slots": CatalogField(
            path="payload.slots",
            type="list",
            label="Slots",
            variable=True,
            item_where=[Condition(field="open", op="is", value="yes")],
        )
    }
    slots = [
        {"label": "A", "value": "a", "open": "yes"},
        {"label": "B", "value": "b", "open": "no"},
    ]
    _record(monkeypatch, _letter({"slots": slots}), fields=fields)
    found = await record_events.declared_lists("pragathi", "e-1", {"slots"})
    assert found == {"slots": [slots[0]]}


# --- the gate probes the person's own phone and email on a Xyne send ----------


def _customer(**fields: Any) -> SimpleNamespace:
    return SimpleNamespace(**{"phone": None, "email": None, **fields})


def _gate_wire(monkeypatch, customer: Any) -> SimpleNamespace:
    probed: List[Dict[str, str]] = []
    looked: List[Any] = []

    async def get_customer(merchant_id: str, customer_id: str) -> Any:
        looked.append((merchant_id, customer_id))
        return customer

    async def is_suppressed(handles: Dict[str, str]) -> bool:
        probed.append(handles)
        return False

    monkeypatch.setattr(gate_module, "get_customer", get_customer)
    monkeypatch.setattr(gate_module, "is_suppressed", is_suppressed)
    return SimpleNamespace(probed=probed, looked=looked)


async def test_a_xyne_send_also_asks_about_the_persons_phone_and_email(
    monkeypatch,
) -> None:
    """A "*" STOP given by phone reaches every channel (canon T02 col 5;
    design/gate-mechanics.md §3.2: the book is probed by the customer's own
    handle values) — so a person who said STOP by phone gets no Xyne form."""
    wired = _gate_wire(
        monkeypatch, _customer(phone="+919812345678", email="Asha@Example.com")
    )
    assert await gate_module.gate(_message()) is None
    assert wired.probed == [
        {"xyne_user_id": ASHA, "phone": "+919812345678", "email": "asha@example.com"}
    ]
    assert wired.looked == [("pragathi", "c-asha")]


async def test_a_suppressed_phone_blocks_the_xyne_send(monkeypatch) -> None:
    async def get_customer(merchant_id: str, customer_id: str) -> Any:
        return _customer(phone="+919812345678")

    async def is_suppressed(handles: Dict[str, str]) -> bool:
        return handles.get("phone") == "+919812345678"

    monkeypatch.setattr(gate_module, "get_customer", get_customer)
    monkeypatch.setattr(gate_module, "is_suppressed", is_suppressed)
    assert await gate_module.gate(_message()) == "suppressed"


async def test_a_whatsapp_send_is_probed_as_before(monkeypatch) -> None:
    """The phone IS the address there: no customer read, one handle."""
    wired = _gate_wire(monkeypatch, _customer(email="a@example.com"))
    message = _message(channel="whatsapp", sent_to_address="+919812345678")
    assert await gate_module.gate(message) is None
    assert wired.probed == [{"phone": "+919812345678"}]
    assert wired.looked == []


async def test_a_stored_value_no_stop_could_match_is_left_out(monkeypatch) -> None:
    """The book holds normalized values only; a junk stored phone could
    never be under a STOP, and must not turn the probe into a refusal."""
    wired = _gate_wire(monkeypatch, _customer(phone="not a phone"))
    assert await gate_module.gate(_message()) is None
    assert wired.probed == [{"xyne_user_id": ASHA}]


async def test_a_xyne_send_whose_customer_is_missing_closes_the_gate(
    monkeypatch,
) -> None:
    """Missing is NO: without the person, a STOP filed under their phone or
    email cannot be checked, so the send is blocked, not checked by the
    address alone."""
    wired = _gate_wire(monkeypatch, None)
    assert await gate_module.gate(_message()) == "gate_unavailable"
    assert wired.probed == []


async def test_a_customer_read_that_fails_closes_the_gate(monkeypatch) -> None:
    async def get_customer(merchant_id: str, customer_id: str) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(gate_module, "get_customer", get_customer)
    assert await gate_module.gate(_message()) == "gate_unavailable"


def test_087_waits_at_most_three_seconds_for_its_lock() -> None:
    """Waiting for the ACCESS EXCLUSIVE lock queues every gate probe behind
    it, and a probe past its deadline is a terminal 'blocked' — so 087
    gives up and is re-run instead."""
    sql = Path("app/database/migrations/087_platform_identity_xyne_user_id.sql")
    text = sql.read_text()
    assert "SET LOCAL lock_timeout = '3s';" in text
    assert text.index("SET LOCAL lock_timeout") < text.index("ALTER TABLE")

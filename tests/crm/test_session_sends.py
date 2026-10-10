"""send_session: a free-form reply inside the customer-service window.

Pinned here: only Buddy or a teammate may make one and it is always a
'service' message; the row is written in flight with no claim (so the
dispatcher can never pick it up and re-send words it does not have); the
words ride the message.queued letter; the gate and the send door apply
exactly as to a template; a repeated key sends nothing; and every outcome
comes back as a result, never a raise.
"""

import re
from typing import Any, Dict, List, Optional

import pytest

from app.crm.connectivity import dispatch, session
from app.crm.connectivity.db.queries.message import (
    abandon_stale_session_sends_query,
    claim_queued_messages_query,
    insert_session_message_query,
    requeue_stale_claims_query,
)
from app.crm.connectivity.letters import KIND_TEMPLATE, queued_letter_payload
from app.crm.connectivity.reasons import REASON_SESSION_ABANDONED
from app.crm.connectivity.schemas.message import (
    ButtonsBody,
    MessageState,
    ReplyButton,
    SendOutcome,
    TextBody,
)
from app.crm.connectivity.send import resolve_send_route
from app.crm.connectivity.status import MESSAGE_DEAD, MESSAGE_QUEUED, MESSAGE_SENDING
from tests.crm.test_connectivity_adapters import (
    _binding,
    _credential,
    _FakeAccessor,
    _installation,
    _patch_accessors,
    _patch_credential,
)


def _call(**overrides: Any) -> Dict[str, Any]:
    fields: Dict[str, Any] = dict(
        merchant_id="shop",
        customer_id="c-1",
        channel="whatsapp",
        address="98765 43210",
        body=TextBody(text="Your order ships tomorrow"),
        source_kind="agent",
        source_id="sess-1",
        purpose_key="service.conversation",
        dedupe_key="turn-1:0",
    )
    fields.update(overrides)
    return fields


class _World:
    """Everything send_session touches, recorded."""

    def __init__(self, outcome: SendOutcome, suppressed: bool = False) -> None:
        self.outcome = outcome
        self.suppressed = suppressed
        self.inserted: List[tuple] = []
        self.letters: List[Dict[str, Any]] = []
        self.sent: List[tuple] = []
        self.applied: List[tuple] = []
        self.existing: Optional[MessageState] = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def insert(*args: Any) -> Optional[str]:
            self.inserted.append(args)
            return None if self.existing is not None else "m-1"

        async def state(merchant_id: str, dedupe_key: str) -> Optional[MessageState]:
            return self.existing

        async def letter(**fields: Any) -> str:
            self.letters.append(fields)
            return "evt-1"

        async def probe(handles: Dict[str, str]) -> bool:
            return self.suppressed

        async def deliver(token: Any, message: Any, body: Any) -> SendOutcome:
            self.sent.append((token, message, body))
            return self.outcome

        async def apply(*args: Any, **kwargs: Any) -> bool:
            self.applied.append((args, kwargs))
            return True

        monkeypatch.setattr(session.message_accessor, "insert_session_message", insert)
        monkeypatch.setattr(session.message_accessor, "message_state_by_dedupe", state)
        monkeypatch.setattr(session.message_accessor, "apply_outcome", apply)
        monkeypatch.setattr(session, "file_queued_letter", letter)
        monkeypatch.setattr(session, "deliver_session_send", deliver)
        monkeypatch.setattr(dispatch, "is_suppressed", probe)


# --- what a session send may be ---------------------------------------------


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        (dict(source_kind="workflow"), "free-form reply comes from"),
        (dict(purpose_key="utility.order"), "service"),
        (dict(channel="carrier_pigeon"), "does not carry"),
        (dict(address="hello"), "unusable"),
    ],
)
def test_a_proposal_the_rules_refuse_raises_before_anything_is_written(
    fields, match
) -> None:
    call = _call(**fields)
    with pytest.raises(ValueError, match=match):
        session.validate_session_send(
            call["channel"], call["address"], call["source_kind"], call["purpose_key"]
        )


def test_a_teammate_may_reply_and_the_address_is_normalised() -> None:
    assert (
        session.validate_session_send(
            "whatsapp", "98765 43210", "human", "service.conversation"
        )
        == "+919876543210"
    )


# --- the outcome, settled ----------------------------------------------------


def test_a_session_outcome_is_always_settled_never_queued() -> None:
    accepted = session.plan_for_session_outcome(
        SendOutcome(status="accepted", provider_message_id="wamid.1")
    )
    assert (accepted.status, accepted.mark_sent) == ("accepted", True)
    blocked = session.plan_for_session_outcome(
        SendOutcome(status="blocked", reason="suppressed")
    )
    assert (blocked.status, blocked.reason, blocked.retryable) == (
        "blocked",
        "suppressed",
        False,
    )
    flaky = session.plan_for_session_outcome(
        SendOutcome(status="failed", reason="130429", retryable=True)
    )
    # Retryable is handed back to the caller; the row says 'failed', never
    # 'queued' — the dispatcher could not re-send it without the words.
    assert (flaky.status, flaky.retryable) == ("failed", True)


# --- the flow ----------------------------------------------------------------


async def test_a_reply_is_written_filed_sent_and_closed(monkeypatch) -> None:
    world = _World(
        SendOutcome(status="accepted", provider_message_id="wamid.9", binding_id="b-1")
    )
    world.install(monkeypatch)
    result = await session.send_session(**_call(binding_id="b-1"))

    assert result.status == "accepted" and result.message_id == "m-1"
    assert result.provider_message_id == "wamid.9" and not result.duplicate
    # One row, normalized, no template.
    assert world.inserted == [
        (
            "shop",
            "c-1",
            "whatsapp",
            "+919876543210",
            "agent",
            "sess-1",
            "service.conversation",
            "turn-1:0",
        )
    ]
    # The words ride the letter, filed alongside the send itself.
    assert len(world.letters) == 1
    assert world.letters[0]["body"] == TextBody(text="Your order ships tomorrow")
    assert world.letters[0]["template_id"] is None
    # The door got a grant naming THIS message, and the pipe the caller named.
    token, message, body = world.sent[0]
    assert token.message_id == "m-1" and token.granted
    assert message.binding_id == "b-1" and message.attempt == 1
    # Closed on its own claim generation (attempt 1), stamped sent, pipe kept.
    args, kwargs = world.applied[0]
    assert args[:6] == ("m-1", "accepted", None, "wamid.9", True, 1)
    assert kwargs["binding_id"] == "b-1"


async def test_the_same_key_twice_sends_nothing_the_second_time(monkeypatch) -> None:
    world = _World(SendOutcome(status="accepted"))
    world.existing = MessageState(
        id="m-0", status="accepted", provider_message_id="wamid.0"
    )
    world.install(monkeypatch)
    result = await session.send_session(**_call())
    assert result.duplicate and result.message_id == "m-0"
    assert result.status == "accepted"
    assert world.letters == [] and world.sent == [] and world.applied == []


async def test_someone_who_said_stop_is_not_messaged(monkeypatch) -> None:
    world = _World(SendOutcome(status="accepted"), suppressed=True)
    world.install(monkeypatch)
    result = await session.send_session(**_call())
    assert (result.status, result.reason) == ("blocked", "suppressed")
    assert world.sent == []
    # The row still closes — blocked, unsent — and the letter was filed:
    # the timeline shows a reply that was refused, with its words.
    assert world.applied[0][0][1] == "blocked"
    assert len(world.letters) == 1


async def test_a_retryable_failure_is_handed_back_not_retried(monkeypatch) -> None:
    world = _World(SendOutcome(status="failed", reason="130429", retryable=True))
    world.install(monkeypatch)
    result = await session.send_session(**_call())
    assert (result.status, result.reason, result.retryable) == (
        "failed",
        "130429",
        True,
    )
    assert world.applied[0][0][1] == "failed"


async def test_a_raising_door_is_an_outcome_not_a_raise(monkeypatch) -> None:
    world = _World(SendOutcome(status="accepted"))
    world.install(monkeypatch)

    async def explode(*args: Any) -> SendOutcome:
        raise RuntimeError("boom")

    monkeypatch.setattr(session, "deliver_session_send", explode)
    result = await session.send_session(**_call())
    assert (result.status, result.reason, result.retryable) == (
        "failed",
        "send_error",
        True,
    )


async def test_a_row_that_cannot_be_closed_still_reports_what_the_provider_did(
    monkeypatch,
) -> None:
    world = _World(SendOutcome(status="accepted", provider_message_id="wamid.5"))
    world.install(monkeypatch)

    async def broken(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("pool gone")

    monkeypatch.setattr(session.message_accessor, "apply_outcome", broken)
    result = await session.send_session(**_call())
    assert result.status == "accepted" and result.provider_message_id == "wamid.5"


# --- the row the dispatcher can never claim ------------------------------------


def test_a_session_row_is_written_in_flight_with_no_claim() -> None:
    query, values = insert_session_message_query(
        "shop", "c-1", "whatsapp", "+91", "agent", None, "service.x", "k"
    )
    assert MESSAGE_SENDING in values and MESSAGE_QUEUED not in values
    assert "claimed_at" not in query  # the marker: in flight, never claimed
    assert "ON CONFLICT (merchant_id, dedupe_key) DO NOTHING" in query
    placeholders = set(re.findall(r"\$(\d+)", query))
    assert placeholders == {str(n) for n in range(1, len(values) + 1)}


def test_the_dispatcher_claims_and_reclaims_only_what_it_claimed() -> None:
    """The claim takes 'queued' rows; the stale sweep takes rows whose
    claimed_at is old. A session row is 'sending' with claimed_at NULL —
    neither statement can ever match it."""
    claim, claim_values = claim_queued_messages_query(10)
    assert "WHERE status = $3" in claim and claim_values[2] == MESSAGE_QUEUED
    sweep, _ = requeue_stale_claims_query(15, 3)
    assert "claimed_at < now()" in sweep


def test_an_abandoned_session_row_closes_dead_not_requeued() -> None:
    query, values = abandon_stale_session_sends_query(15)
    assert "claimed_at IS NULL" in query
    assert values == [15, MESSAGE_DEAD, REASON_SESSION_ABANDONED, MESSAGE_SENDING]


# --- the letter --------------------------------------------------------------


def test_the_letter_carries_the_words_and_the_template_carries_none() -> None:
    reply = queued_letter_payload(
        message_id="m-1",
        channel="whatsapp",
        sent_to_address="+91",
        source_kind="agent",
        source_id=None,
        purpose_key="service.conversation",
        template_id=None,
        variables={},
        body=ButtonsBody(text="Size?", buttons=[ReplyButton(id="s8", title="8")]),
    )
    assert reply["kind"] == "buttons" and reply["text"] == "Size?"
    assert reply["body"]["buttons"] == [{"id": "s8", "title": "8"}]
    template = queued_letter_payload(
        message_id="m-2",
        channel="whatsapp",
        sent_to_address="+91",
        source_kind="workflow",
        source_id="run-1",
        purpose_key="utility.order",
        template_id="cod_confirm_v2",
        variables={"1": "Priya"},
    )
    assert template["kind"] == KIND_TEMPLATE
    assert template["text"] is None and template["body"] is None


# --- the route ---------------------------------------------------------------


async def test_a_session_route_needs_no_template_but_everything_else(
    monkeypatch,
) -> None:
    """The registry is the one step a free-form reply skips; a missing pipe
    or an unhealthy door refuses it exactly as it refuses a template."""

    class _NoRegistry(_FakeAccessor):
        async def approved_templates_for_send(self, *args: Any, **kwargs: Any):
            raise AssertionError("a session route read the template registry")

    _patch_accessors(
        monkeypatch, _NoRegistry(binding=_binding(), installation=_installation())
    )
    _patch_credential(monkeypatch, _credential())
    route = await resolve_send_route("shop", "whatsapp", None, None, session=True)
    assert not isinstance(route, str) and route.template is None

    _patch_accessors(monkeypatch, _NoRegistry(binding=None))
    assert (
        await resolve_send_route("shop", "whatsapp", None, None, session=True)
        == "no_active_binding"
    )

"""send_reply: a free-form reply inside the customer-service window.

Pinned here: only Buddy or a teammate may make one and it is always a
'service' message; the row is written in flight with no claim (so the
dispatcher can never pick it up and re-send words it does not have); the
words go to the send door and nowhere else; the gate and the send door apply
exactly as to a template; a repeated key sends nothing; and every outcome
comes back as a result, never a raise.
"""

import re
from typing import Any, Dict, List, Optional

import pytest

from app.crm.connectivity.db.queries.message import (
    claim_queued_messages_query,
    insert_reply_message_query,
    mark_abandoned_replies_dead_query,
    requeue_stale_claims_query,
)
from app.crm.connectivity.messaging.conversations import reply
from app.crm.connectivity.messaging.shared import gate
from app.crm.connectivity.schemas.message.shared import MessageState, SendOutcome
from app.crm.connectivity.schemas.message.whatsapp import (
    TextBody,
)
from app.crm.connectivity.send import resolve_send_route
from app.crm.connectivity.vocabulary.reasons import REASON_REPLY_ABANDONED
from app.crm.connectivity.vocabulary.status import (
    MESSAGE_DEAD,
    MESSAGE_QUEUED,
    MESSAGE_SENDING,
)
from tests.crm.test_connectivity_adapters import (
    _binding,
    _credential,
    _FakeAccessor,
    _installation,
    _patch_accessors,
    _patch_credential,
)


def _send_reply_kwargs(**overrides: Any) -> Dict[str, Any]:
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


class _ReplySendFakes:
    """Everything send_reply touches, recorded."""

    def __init__(self, outcome: SendOutcome, suppressed: bool = False) -> None:
        self.outcome = outcome
        self.suppressed = suppressed
        self.inserted: List[tuple] = []
        self.sent: List[tuple] = []
        self.applied: List[tuple] = []
        self.existing: Optional[MessageState] = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_insert_reply_message(*args: Any) -> Optional[str]:
            self.inserted.append(args)
            return None if self.existing is not None else "m-1"

        async def fake_get_message_state_by_dedupe_key(
            merchant_id: str, dedupe_key: str
        ) -> Optional[MessageState]:
            return self.existing

        async def fake_is_suppressed(handles: Dict[str, str]) -> bool:
            return self.suppressed

        async def fake_send_with_reply_body(
            token: Any, message: Any, body: Any
        ) -> SendOutcome:
            self.sent.append((token, message, body))
            return self.outcome

        async def fake_apply_outcome(*args: Any, **kwargs: Any) -> bool:
            self.applied.append((args, kwargs))
            return True

        monkeypatch.setattr(
            reply.message_accessor, "insert_reply_message", fake_insert_reply_message
        )
        monkeypatch.setattr(
            reply.message_accessor,
            "get_message_state_by_dedupe_key",
            fake_get_message_state_by_dedupe_key,
        )
        monkeypatch.setattr(reply.message_accessor, "apply_outcome", fake_apply_outcome)
        monkeypatch.setattr(reply, "send_with_reply_body", fake_send_with_reply_body)
        monkeypatch.setattr(gate, "is_suppressed", fake_is_suppressed)


# --- what a reply send may be -----------------------------------------------


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
    call = _send_reply_kwargs(**fields)
    with pytest.raises(ValueError, match=match):
        reply.validate_reply(
            call["channel"], call["address"], call["source_kind"], call["purpose_key"]
        )


def test_a_teammate_may_reply_and_the_address_is_normalised() -> None:
    assert (
        reply.validate_reply("whatsapp", "98765 43210", "human", "service.conversation")
        == "+919876543210"
    )


# --- the outcome, settled ----------------------------------------------------


def test_a_reply_outcome_is_always_settled_never_queued() -> None:
    accepted = reply.plan_for_reply_outcome(
        SendOutcome(status="accepted", provider_message_id="wamid.1")
    )
    assert (accepted.status, accepted.mark_sent) == ("accepted", True)
    blocked = reply.plan_for_reply_outcome(
        SendOutcome(status="blocked", reason="suppressed")
    )
    assert (blocked.status, blocked.reason, blocked.retryable) == (
        "blocked",
        "suppressed",
        False,
    )
    flaky = reply.plan_for_reply_outcome(
        SendOutcome(status="failed", reason="130429", retryable=True)
    )
    # Retryable is handed back to the caller; the row says 'failed', never
    # 'queued' — the dispatcher could not re-send it without the words.
    assert (flaky.status, flaky.retryable) == ("failed", True)


# --- the flow ----------------------------------------------------------------


async def test_a_reply_is_written_sent_and_closed(monkeypatch) -> None:
    fakes = _ReplySendFakes(
        SendOutcome(status="accepted", provider_message_id="wamid.9", binding_id="b-1")
    )
    fakes.install(monkeypatch)
    result = await reply.send_reply(**_send_reply_kwargs(binding_id="b-1"))

    assert result.status == "accepted" and result.message_id == "m-1"
    assert result.provider_message_id == "wamid.9" and not result.duplicate
    # One row, normalized, no template.
    assert fakes.inserted == [
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
    # The door got a grant naming THIS message, the pipe the caller named,
    # and the words — they go nowhere else.
    token, message, body = fakes.sent[0]
    assert body == TextBody(text="Your order ships tomorrow")
    assert token.message_id == "m-1" and token.granted
    assert message.binding_id == "b-1" and message.attempt == 1
    # Closed on its own claim generation (attempt 1), stamped sent, pipe kept.
    args, kwargs = fakes.applied[0]
    assert args[:6] == ("m-1", "accepted", None, "wamid.9", True, 1)
    assert kwargs["binding_id"] == "b-1"


async def test_the_same_key_twice_sends_nothing_the_second_time(monkeypatch) -> None:
    fakes = _ReplySendFakes(SendOutcome(status="accepted"))
    fakes.existing = MessageState(
        id="m-0", status="accepted", provider_message_id="wamid.0"
    )
    fakes.install(monkeypatch)
    result = await reply.send_reply(**_send_reply_kwargs())
    assert result.duplicate and result.message_id == "m-0"
    assert result.status == "accepted"
    assert fakes.sent == [] and fakes.applied == []


async def test_someone_who_said_stop_is_not_messaged(monkeypatch) -> None:
    fakes = _ReplySendFakes(SendOutcome(status="accepted"), suppressed=True)
    fakes.install(monkeypatch)
    result = await reply.send_reply(**_send_reply_kwargs())
    assert (result.status, result.reason) == ("blocked", "suppressed")
    assert fakes.sent == []
    # The row still closes — blocked, unsent.
    assert fakes.applied[0][0][1] == "blocked"


async def test_a_retryable_failure_is_handed_back_not_retried(monkeypatch) -> None:
    fakes = _ReplySendFakes(
        SendOutcome(status="failed", reason="130429", retryable=True)
    )
    fakes.install(monkeypatch)
    result = await reply.send_reply(**_send_reply_kwargs())
    assert (result.status, result.reason, result.retryable) == (
        "failed",
        "130429",
        True,
    )
    assert fakes.applied[0][0][1] == "failed"


async def test_a_raising_door_is_an_outcome_not_a_raise(monkeypatch) -> None:
    fakes = _ReplySendFakes(SendOutcome(status="accepted"))
    fakes.install(monkeypatch)

    async def explode(*args: Any) -> SendOutcome:
        raise RuntimeError("boom")

    monkeypatch.setattr(reply, "send_with_reply_body", explode)
    result = await reply.send_reply(**_send_reply_kwargs())
    assert (result.status, result.reason, result.retryable) == (
        "failed",
        "send_error",
        True,
    )


async def test_a_row_that_cannot_be_closed_still_reports_what_the_provider_did(
    monkeypatch,
) -> None:
    fakes = _ReplySendFakes(
        SendOutcome(status="accepted", provider_message_id="wamid.5")
    )
    fakes.install(monkeypatch)

    async def broken(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("pool gone")

    monkeypatch.setattr(reply.message_accessor, "apply_outcome", broken)
    result = await reply.send_reply(**_send_reply_kwargs())
    assert result.status == "accepted" and result.provider_message_id == "wamid.5"


# --- the row the dispatcher can never claim ------------------------------------


def test_a_reply_row_is_written_in_flight_with_no_claim() -> None:
    query, values = insert_reply_message_query(
        "shop", "c-1", "whatsapp", "+91", "agent", None, "service.x", "k"
    )
    assert MESSAGE_SENDING in values and MESSAGE_QUEUED not in values
    assert "claimed_at" not in query  # the marker: in flight, never claimed
    assert "ON CONFLICT (merchant_id, dedupe_key) DO NOTHING" in query
    placeholders = set(re.findall(r"\$(\d+)", query))
    assert placeholders == {str(n) for n in range(1, len(values) + 1)}


def test_the_dispatcher_claims_and_reclaims_only_what_it_claimed() -> None:
    """The claim takes 'queued' rows; the stale sweep takes rows whose
    claimed_at is old. A reply row is 'sending' with claimed_at NULL —
    neither statement can ever match it."""
    claim, claim_values = claim_queued_messages_query(10)
    assert "WHERE status = $3" in claim and claim_values[2] == MESSAGE_QUEUED
    sweep, _ = requeue_stale_claims_query(15, 3)
    assert "claimed_at < now()" in sweep


def test_an_abandoned_reply_row_closes_dead_not_requeued() -> None:
    query, values = mark_abandoned_replies_dead_query(15)
    assert "claimed_at IS NULL" in query
    assert values == [15, MESSAGE_DEAD, REASON_REPLY_ABANDONED, MESSAGE_SENDING]


# --- the route ---------------------------------------------------------------


async def test_a_reply_route_needs_no_template_but_everything_else(
    monkeypatch,
) -> None:
    """The registry is the one step a free-form reply skips; a missing pipe
    or an unhealthy door refuses it exactly as it refuses a template."""

    class _NoRegistry(_FakeAccessor):
        async def approved_templates_for_send(self, *args: Any, **kwargs: Any):
            raise AssertionError("a reply route read the template registry")

    _patch_accessors(
        monkeypatch, _NoRegistry(binding=_binding(), installation=_installation())
    )
    _patch_credential(monkeypatch, _credential())
    route = await resolve_send_route("shop", "whatsapp", None, None, is_reply=True)
    assert not isinstance(route, str) and route.template is None

    _patch_accessors(monkeypatch, _NoRegistry(binding=None))
    assert (
        await resolve_send_route("shop", "whatsapp", None, None, is_reply=True)
        == "no_active_binding"
    )

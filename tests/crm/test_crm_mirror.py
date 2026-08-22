"""Mirror mechanics: topic-qualified dedupe keys, non-customer-traffic
exclusion (pilot merchants' customer lists must never fill with test
numbers, transport-only sessions, or consult-leg staff numbers), and the
stamp pass-through law: mirrors carry an already-resolved customer_id,
they never resolve."""

import asyncio
from typing import Any, Dict, List, Optional

import pytest

import app.ai.voice.agents.breeze_buddy.crm_mirror as crm_mirror
from app.ai.voice.agents.breeze_buddy.crm_mirror import (
    _created_lead_tap,
    _event_key,
    is_non_customer_lead,
)
from app.schemas import CallDirection, LeadCallTracker


def test_event_key_qualifies_by_topic() -> None:
    # One call SID covers the attempt AND the completion — unqualified,
    # the second would be dropped as a redelivery.
    sid = "CA123"
    assert _event_key("call.attempted", sid) != _event_key("call.completed", sid)
    assert _event_key("call.attempted", sid) == "call.attempted:CA123"


def test_test_execution_modes_are_excluded() -> None:
    assert is_non_customer_lead("TELEPHONY_TEST", None) is True
    assert is_non_customer_lead("DAILY_TEST", {}) is True


def test_non_customer_production_modes_are_excluded() -> None:
    # DAILY_STREAM is a transport-only service — client-driven payloads,
    # its numbers are not trusted identities.
    assert is_non_customer_lead("DAILY_STREAM", None) is True


def test_customer_execution_modes_are_mirrored() -> None:
    assert is_non_customer_lead("TELEPHONY", {}) is False
    assert is_non_customer_lead("DAILY", {}) is False
    # HOLD_TRANSFER mirrors: live hold-transfer configs dial real
    # customers (the ride booker), not staff.
    assert is_non_customer_lead("HOLD_TRANSFER", {}) is False


def test_playground_leads_are_excluded() -> None:
    assert is_non_customer_lead("TELEPHONY", {"playground": True}) is True
    assert is_non_customer_lead("TELEPHONY", {"playground": False}) is False


def test_enum_like_execution_mode() -> None:
    class Mode:
        value = "DAILY_TEST"

    assert is_non_customer_lead(Mode(), None) is True


def test_lead_model_carries_customer_stamp() -> None:
    # The taps pass lead.customer_id through to mirrors — the model must
    # expose the column migration 050 added, defaulting None.
    lead = LeadCallTracker(id="L1", reseller_id="r1", template="t")
    assert lead.customer_id is None


def test_inbound_mirror_is_born_attributed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The created tap sequences resolve -> stamp -> call.inbound mirror in
    one task, so the event carries the customer_id instead of racing the
    stamp and recording NULL (the bug an inbound pilot call surfaced)."""
    recorded: List[Dict[str, Any]] = []

    async def fake_resolve(merchant_id: str, handles: Any, **kw: Any) -> str:
        return "cust-42"

    async def fake_stamp(lead_id: str, customer_id: str) -> bool:
        return True

    async def fake_record_event(**kw: Any) -> None:
        recorded.append(kw)

    def run_now(coro: Any, name: Optional[str] = None) -> None:
        asyncio.run(coro)

    monkeypatch.setattr(crm_mirror, "crm_resolve", fake_resolve)
    monkeypatch.setattr(crm_mirror.lct_accessor, "stamp_lead_customer", fake_stamp)
    monkeypatch.setattr(crm_mirror, "record_event", fake_record_event)
    monkeypatch.setattr(crm_mirror, "spawn_background_task", run_now)

    lead = LeadCallTracker(
        id="L1",
        reseller_id="r1",
        template="t",
        merchant_id="m1",
        call_id="CA123",
        call_direction=CallDirection.INBOUND,
        payload={"customer_mobile_number": "+919999999999"},
    )
    _created_lead_tap(lead)

    assert len(recorded) == 1
    event = recorded[0]
    assert event["topic"] == "call.inbound"
    assert event["customer_id"] == "cust-42"
    assert event["external_id"] == "call.inbound:CA123"

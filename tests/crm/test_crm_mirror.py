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
    _finished_lead_tap,
    is_non_customer_lead,
)
from app.database.decoder.breeze_buddy.lead_call_tracker import (
    decode_lead_call_tracker,
)
from app.schemas import CallDirection, LeadCallStatus, LeadCallTracker


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


# --- rollout phase 18: a call's outcome reaches the run that placed it ---


def test_lead_model_and_decoder_carry_the_walkers_enrollment_stamp() -> None:
    """Migration 059's column, exposed so the finished tap can pass it on
    (the queries return whole rows; only the model and decoder lacked it)."""
    lead = LeadCallTracker(id="L1", reseller_id="r1", template="t")
    assert lead.enrollment_id is None
    row = {
        "id": "L1",
        "telephony_number_id": None,
        "reseller_id": "r1",
        "template": "t",
        "template_id": None,
        "merchant_id": "m1",
        "request_id": None,
        "attempt_count": 0,
        "next_attempt_at": None,
        "payload": None,
        "meta_data": None,
        "recording_url": None,
        "status": "FINISHED",
        "outcome": "NO_ANSWER",
        "call_id": "CA9",
        "call_initiated_time": None,
        "call_end_time": None,
        "cost": None,
        "is_locked": False,
        "langfuse_scores": None,
        "execution_mode": "TELEPHONY",
        "call_direction": "OUTBOUND",
        "customer_id": None,
        "enrollment_id": "5c1d3a9e-0000-4000-8000-000000000001",
        "created_at": None,
        "updated_at": None,
    }
    decoded = decode_lead_call_tracker(row)  # type: ignore[arg-type]
    assert decoded is not None
    assert decoded.enrollment_id == "5c1d3a9e-0000-4000-8000-000000000001"


def test_call_completed_mirror_names_the_run_and_the_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 18 (G2): the finished tap's call.completed carries the run
    that placed the call (enrollment_id) and the outcome, so a listening
    square after a call node can branch on THIS run's outcome — a customer
    can have two runs, and one call's outcome must not wake the other's."""
    recorded: List[Dict[str, Any]] = []

    async def fake_record_event(**kw: Any) -> None:
        recorded.append(kw)

    def run_now(coro: Any, name: Optional[str] = None) -> None:
        asyncio.run(coro)

    monkeypatch.setattr(crm_mirror, "record_event", fake_record_event)
    monkeypatch.setattr(crm_mirror, "spawn_background_task", run_now)
    lead = LeadCallTracker(
        id="L1",
        reseller_id="r1",
        template="t",
        merchant_id="m1",
        call_id="CA9",
        customer_id="cust-1",
        enrollment_id="run-1",
        outcome="NO_ANSWER",
        payload={"customer_mobile_number": "+919999999999"},
    )
    _finished_lead_tap(lead)
    (event,) = recorded
    assert event["topic"] == "call.completed" and event["customer_id"] == "cust-1"
    assert event["payload"]["enrollment_id"] == "run-1"
    assert event["payload"]["outcome"] == "NO_ANSWER"
    assert event["payload"]["lead_id"] == "L1"
    # a lead no run placed says nothing about a run (None is dropped)
    recorded.clear()
    _finished_lead_tap(
        LeadCallTracker(
            id="L2", reseller_id="r1", template="t", merchant_id="m1", call_id="CA10"
        )
    )
    assert "enrollment_id" not in recorded[0]["payload"]


# --------------------------------------------------------------------------
# A template's declared answers, and the names a merchant is free to choose
# --------------------------------------------------------------------------


def _completed(**over: Any) -> LeadCallTracker:
    """A finished TELEPHONY lead, the shape the completed tap fires on."""
    fields: Dict[str, Any] = {
        "id": "L9",
        "reseller_id": "r1",
        "template": "t",
        "template_id": "tpl-1",
        "merchant_id": "m1",
        "call_id": "CA999",
        "status": LeadCallStatus.FINISHED,
        "execution_mode": "TELEPHONY",
        "outcome": "ANSWERED",
        "call_direction": CallDirection.OUTBOUND,
        "payload": {"customer_mobile_number": "+919999999999"},
    }
    fields.update(over)
    return LeadCallTracker(**fields)


def _mirror_harness(
    monkeypatch: pytest.MonkeyPatch, schema: Optional[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Everything the completed tap touches, stubbed; returns what was
    recorded. The template read is the only cold one call_facts makes."""
    recorded: List[Dict[str, Any]] = []

    async def fake_record_event(**kw: Any) -> None:
        recorded.append(kw)

    async def fake_get_template(template_id: str) -> Any:
        return type("T", (), {"expected_callback_response_schema": schema})()

    def run_now(coro: Any, name: Optional[str] = None) -> None:
        asyncio.run(coro)

    monkeypatch.setattr(crm_mirror, "record_event", fake_record_event)
    monkeypatch.setattr(crm_mirror, "spawn_background_task", run_now)
    monkeypatch.setattr(
        crm_mirror.template_accessor, "get_template_by_id", fake_get_template
    )
    return recorded


def test_a_declared_answer_named_outcome_still_produces_a_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """call_facts returns MERCHANT-authored names. Splatted beside the
    mirror's own `outcome=` keyword, a template declaring `outcome` raised
    TypeError inside the spawned tap — logged, swallowed, no letter, and the
    run sat until max_age_days. As one dict it cannot collide.
    """
    recorded = _mirror_harness(monkeypatch, {"outcome": {}, "reason": {}})
    lead = _completed(
        metaData={"outcome": {"outcome": "she cancelled", "reason": "cost"}}
    )

    _finished_lead_tap(lead)

    assert len(recorded) == 1, "the letter must still be filed"
    payload = recorded[0]["payload"]
    # OURS wins: the walker and the console read `outcome` off this payload,
    # so the call's own verdict is kept and the declared field is dropped.
    assert payload["outcome"] == "ANSWERED"
    # …and every name that does NOT collide still rides along.
    assert payload["reason"] == "cost"


def test_every_reserved_name_survives_being_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not just `outcome` — the same crash was one declaration away on every
    keyword the completed tap passes.
    """
    reserved = [
        "lead_id",
        "call_id",
        "outcome",
        "enrollment_id",
        "direction",
        "started_at",
        "ended_at",
        "customer_name",
        "customer_mobile_number",
    ]
    for name in reserved:
        recorded = _mirror_harness(monkeypatch, {name: {}})
        _finished_lead_tap(_completed(metaData={"outcome": {name: "declared"}}))
        assert len(recorded) == 1, f"{name} lost the letter"
        assert recorded[0]["payload"].get(name) != "declared", name


def test_the_hooks_nested_claim_outranks_buddys_scratch_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The top level is also buddy's scratch space ("stuck_processing_timeout",
    abort reasons). metaData['outcome'] is the hook's alone, so it wins.
    """
    recorded = _mirror_harness(monkeypatch, {"reason": {}})
    _finished_lead_tap(
        _completed(
            metaData={
                "reason": "stuck_processing_timeout",  # buddy's own word
                "outcome": {"reason": "she found it cheaper"},  # the agent's
            }
        )
    )

    assert recorded[0]["payload"]["reason"] == "she found it cheaper"


def test_a_declared_name_absent_from_the_nest_still_reads_the_top_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template option merges its `metadata` onto lead.metaData with no nest
    at all — that writer must keep working.
    """
    recorded = _mirror_harness(monkeypatch, {"chosen_slot": {}})
    _finished_lead_tap(_completed(metaData={"chosen_slot": "tomorrow 4pm"}))

    assert recorded[0]["payload"]["chosen_slot"] == "tomorrow 4pm"


def test_a_long_declared_answer_reaches_the_spine_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The letter is stored verbatim (T13), so nothing is trimmed here. The
    ceiling that used to live in call_facts belongs to run context, and
    entry.py applies it there.
    """
    long_address = "x" * 300
    recorded = _mirror_harness(monkeypatch, {"updated_address": {}})
    _finished_lead_tap(
        _completed(metaData={"outcome": {"updated_address": long_address}})
    )

    assert recorded[0]["payload"]["updated_address"] == long_address

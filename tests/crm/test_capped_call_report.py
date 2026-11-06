"""A capped call square's lead is born FINISHED/ABORTED, and its report
(call.completed) is minted INSIDE the square's visit — one write before the
walker moves the run onto the wait after it. The consumer's answer depends on
which write lands first. These pin both orders: the accepted window (25 Sep
2026 ruling — a consumer poll between the two writes spends the report and
the wait then sleeps its alarm) and the normal order, which wakes the run."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import uuid4

import pytest

import app.crm.outreach.entry as entry
from app.crm.outreach.nodes.call import ABORTED_OUTCOME
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition
from tests.crm.test_plan_templates import PLANS, _load

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _run(node: str, context: Dict[str, Any]) -> EnrollmentRun:
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=uuid4(),
        workflow_version=1,
        customer_id=uuid4(),
        status="waiting",
        current_node=node,
        wake_at=NOW + timedelta(minutes=30),
        entered_at=NOW - timedelta(hours=1),
        exited_at=None,
        exit_reason=None,
        context=context,
        enrollment_key="c-1",
        attempts=1,
        last_error=None,
        node_arrived_at=NOW - timedelta(minutes=20),
    )


def _report(lead_id: str) -> entry.RawEvent:
    return entry.RawEvent(
        id="ev-abort",
        merchant_id="m1",
        source="telephony",
        topic="call.completed",
        schema_version="1",
        external_id=f"call.completed:{lead_id}",
        payload={"lead_id": lead_id, "outcome": ABORTED_OUTCOME},
        received_at=NOW,
        occurred_at=NOW,
    )


def _consume(monkeypatch: pytest.MonkeyPatch, run: EnrollmentRun) -> List[str]:
    """The report reaches the consumer while the run stands where `run`
    says; returns the squares the resume statement would have touched."""
    resumed: List[str] = []
    refreshed: List[str] = []

    async def resume_run_by_id(
        merchant: str, run_id: str, square: str, *a: Any
    ) -> bool:
        hit = square == run.current_node  # the statement's own WHERE
        if hit:
            resumed.append(square)
        return hit

    async def refresh_run_facts(*args: Any, **kwargs: Any) -> bool:
        refreshed.append(args[2])
        return True

    async def max_chars() -> int:
        return 500

    monkeypatch.setattr(entry.enrollment_accessor, "resume_run_by_id", resume_run_by_id)
    monkeypatch.setattr(
        entry.enrollment_accessor, "refresh_run_facts", refresh_run_facts
    )
    monkeypatch.setattr(entry, "CRM_CONTEXT_VALUE_MAX_CHARS", max_chars)
    definition = WorkflowDefinition.model_validate(
        _load(PLANS / "line-nudge-call-wait.json")
    )
    asyncio.run(entry._wake_on_reply(run, definition, _report("lead-cap")))
    assert refreshed == [], "a report never takes the deaf-square refresh"
    return resumed


def test_the_report_landing_before_the_walkers_advance_reaches_no_square(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE WINDOW. Consumed first: the run still stands on call-1 with LAST
    visit's lead in its context. No listening square matches and nothing
    holds the letter; the walker then arms after-call-1 for its whole alarm.
    Accepted as rare (the consumer's poll must fall between two walker
    writes) and bounded by that alarm."""
    run = _run("call-1", {"customer_id": "c-1", "lead_call-1": "lead-from-last-visit"})
    assert _consume(monkeypatch, run) == []


def test_the_aborted_report_wakes_the_run_standing_on_its_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normal order: the walker's advance landed first, the run stands
    on after-call-1 for the aborted lead, and its report wakes it with
    ABORTED — the wait's `else` arrow, not its alarm."""
    run = _run("after-call-1", {"customer_id": "c-1", "lead_call-1": "lead-cap"})
    assert _consume(monkeypatch, run) == ["after-call-1"]

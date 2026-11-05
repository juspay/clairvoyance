"""A call report answers its square but never becomes the run's latest
letter (22 Sep 2026; docs/crm/runbooks/after-call.md).

A wait square after a call hears that call's `call.completed` (matched on
the lead the call square queued) and branches on the outcome — today's
words, nothing new. What was wrong: every heard letter took the
latest-letter pointer, so the NEXT call was built from the report's facts
and the offers the merchant sent before the first call were gone from the
second (seen live 16 Sep 2026). The report now keeps its facts under its
square and leaves the pointer where the merchant's last word put it.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from uuid import uuid4

import pytest

import app.crm.outreach.entry as entry
from app.ai.voice.agents.breeze_buddy.crm_mirror import MIRRORS
from app.crm.outreach.nodes.context import run_facts
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode
from app.crm.record.contracts import CALL_REPORT_SOURCES, RawEvent
from tests.crm.test_plan_templates import PLANS, _load

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

AFTER_CALL = WorkflowNode.model_validate(
    {
        "id": "after-call-1",
        "type": "wait",
        "topics": ["call.completed"],
        "key": "outcome",
        "minutes": 1440,
        "match": {"payload": "lead_id", "run": "lead_call-1"},
    }
)
QUIET = WorkflowNode.model_validate(
    {"id": "quiet-30m", "type": "wait", "topics": ["OFFERED"], "key": "$topic"}
)


def _event(topic: str, payload: Dict[str, Any], source: str = "flipkart") -> RawEvent:
    return RawEvent(
        id="ev-1",
        merchant_id="m1",
        source=source,
        topic=topic,
        schema_version="1",
        external_id=f"{topic}:ev-1",
        payload=payload,
        received_at=NOW,
        occurred_at=NOW,
    )


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


def test_the_call_report_sources_are_the_mirrors_call_sources() -> None:
    """Pinned to the mirror's own table, not to a literal: rename the
    telephony source there and this goes red, instead of reports quietly
    taking the pointer again (the 16 Sep 2026 bug)."""
    assert CALL_REPORT_SOURCES == {
        source for topic, source in MIRRORS.items() if topic.startswith("call.")
    }


def test_a_call_report_answers_the_square_but_never_takes_the_latest_letter() -> None:
    report = _event(
        "call.completed", {"lead_id": "lead-1", "outcome": "NO_ANSWER"}, "telephony"
    )
    assert entry._reply_patch(AFTER_CALL, report, "NO_ANSWER") == {
        "reply_after-call-1": "NO_ANSWER",
        "cut_short_by": "ev-1",
    }
    letter = _event("OFFERED", {"customer_id": "c-1"})
    assert entry._reply_patch(QUIET, letter, "OFFERED") == {
        "reply_quiet-30m": "OFFERED",
        "cut_short_by": "ev-1",
        "latest_letter": "quiet-30m",
    }


def test_the_after_call_square_hears_only_the_call_it_queued() -> None:
    """The call square writes lead_<square> into context; the report carries
    lead_id; `match` compares them as text. A lead id is uuid5 of run, square
    and visit, so no other run, square or visit can answer to it."""
    run = _run("after-call-1", {"customer_id": "c-1", "lead_call-1": "lead-1"})
    mine = _event(
        "call.completed", {"lead_id": "lead-1", "outcome": "BUSY"}, "telephony"
    )
    other_call = _event("call.completed", {"lead_id": "lead-2"}, "telephony")
    no_lead = _event("call.completed", {"outcome": "BUSY"}, "telephony")
    assert entry._is_about(AFTER_CALL, mine, run)
    assert not entry._is_about(AFTER_CALL, other_call, run)
    assert not entry._is_about(AFTER_CALL, no_lead, run)
    assert entry._answer_for(AFTER_CALL, mine) == "BUSY"


def test_the_next_call_speaks_the_merchants_facts_not_the_reports() -> None:
    """What the fix changes for call-2's payload: the merchant's letter on
    quiet-30m stays the latest word; the report sits under its own square."""
    letter_facts = {"offers": "1. Bank: 12 months", "customer_id": "c-1"}
    report_facts = {"outcome": "NO_ANSWER", "lead_id": "lead-1"}
    context = {
        "customer_id": "c-1",
        "phone": "+919876543210",
        "latest_letter": "quiet-30m",  # the letter's pointer, left alone by the report
        "facts": {"quiet-30m": letter_facts, "after-call-1": report_facts},
    }
    facts = run_facts(context)
    assert facts["offers"] == "1. Bank: 12 months"
    assert "outcome" not in facts  # the report's scalars are not the merchant's
    assert facts["facts_after-call-1_outcome"] == "NO_ANSWER"  # but readable by name
    # had the report taken the pointer (the old behaviour), call-2 would have
    # spoken the outcome and lost nothing of the offers only by luck of naming
    taken = {**context, "latest_letter": "after-call-1"}
    assert run_facts(taken)["outcome"] == "NO_ANSWER"


def test_the_example_plan_waits_for_each_calls_own_report() -> None:
    plan = _load(PLANS / "line-nudge-call-wait.json")
    assert validate_definition(plan) == []
    squares = {n["id"]: n for n in plan["nodes"]}
    for i in ("1", "2"):
        after = squares[f"after-call-{i}"]
        assert after["topics"] == ["call.completed"] and after["key"] == "outcome"
        assert after["match"] == {"payload": "lead_id", "run": f"lead_call-{i}"}
        assert after["minutes"] == 1440 and "window" not in after
        assert [e[0] for e in plan["edges"] if e[1] == f"after-call-{i}"] == [
            f"call-{i}"
        ]
        labels = sorted(e[2] for e in plan["edges"] if e[0] == f"after-call-{i}")
        assert labels[-2:] == ["else", "timeout"]
    # the outcome arrows are the call template's words: a customer who said
    # no is not called again — straight to listen; everything else, the gap
    stop = {e[2] for e in plan["edges"] if e[0] == "after-call-1" and e[1] == "listen"}
    assert stop == {"NOT_INTERESTED", "CANCEL", "CONFIRM"}
    assert {e[1] for e in plan["edges"] if e[0] == "after-call-1"} == {
        "listen",
        "gap-30m",
    }
    assert {e[1] for e in plan["edges"] if e[0] == "after-call-2"} == {"listen"}
    # the call squares are today's: no waiting words on them
    for i in ("1", "2"):
        assert set(squares[f"call-{i}"]) == {"id", "type", "template_id"}


def test_a_late_report_finding_the_run_on_a_deaf_square_refreshes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second path (Swaroop, 23 Sep 2026): the backstop fired, the run is
    parked on call-2, and call-1's report arrives at last. It matched no
    listening square (the run left after-call-1), and it must not take the
    deaf-square refresh either — that would put `outcome` at the top level
    and un-park the run. A merchant letter on the same square still does."""
    plan = _load(PLANS / "line-nudge-call-wait.json")
    definition = WorkflowDefinition.model_validate(plan)
    run = _run("call-2", {"customer_id": "c-1", "lead_call-1": "lead-1"})
    refreshed: list = []

    async def refresh_run_facts(*args: Any, **kwargs: Any) -> bool:
        refreshed.append(args[2])
        return True

    async def resume_run_by_id(
        merchant: str, run_id: str, square: str, *a: Any
    ) -> bool:
        return square == run.current_node  # the statement's own condition

    async def max_chars() -> int:
        return 500

    monkeypatch.setattr(
        entry.enrollment_accessor, "refresh_run_facts", refresh_run_facts
    )
    monkeypatch.setattr(entry.enrollment_accessor, "resume_run_by_id", resume_run_by_id)
    monkeypatch.setattr(entry, "CRM_CONTEXT_VALUE_MAX_CHARS", max_chars)

    late = _event(
        "call.completed", {"lead_id": "lead-1", "outcome": "NO_ANSWER"}, "telephony"
    )
    asyncio.run(entry._wake_on_reply(run, definition, late))
    assert refreshed == []
    letter = _event("LINE_OFFERED", {"customer_id": "c-1", "offers": "2. New"})
    asyncio.run(entry._wake_on_reply(run, definition, letter))
    assert refreshed == ["call-2"]

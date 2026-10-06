"""A call's rank: decided by the plan's `priority` block, carried on the lead.

The call square works the rank out when it queues its lead and writes it on the
lead's meta_data, where the dialler reads it. A plan with no `priority` block is
exactly as it was.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pytest

import app.crm.outreach.nodes.call as call_node
from app.crm.outreach.entry import _context_from_payload
from app.crm.outreach.nodes.context import run_facts
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.priority import rank_for
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition

IST = ZoneInfo("Asia/Kolkata")
PRIORITY: Dict[str, Any] = {
    "window": {"opens": "10:00", "closes": "20:56", "timezone": "Asia/Kolkata"},
    "ranks": {"1": "first_ready", "2": "newest_event", "3": "newest_event"},
    "rules": [
        {
            "if": [{"field": "run.latest_event_today", "op": "is", "value": True}],
            "rank": 1,
        },
        {
            "if": [
                {"field": "run.latest_topic", "op": "is", "value": "LINE_KYC_COMPLETED"}
            ],
            "rank": 2,
        },
    ],
    "else": 3,
}


def _plan(**overrides: Any) -> Dict[str, Any]:
    """The line-nudge board in small: a quiet wait that hears the stage letters,
    a call, and the wait that hears that call's report."""
    base: Dict[str, Any] = {
        "entry": {"topic": "LINE_OFFERED", "reenter": True, "cooldown_hours": 0},
        "nodes": [
            {
                "id": "quiet",
                "type": "wait",
                "topics": ["LINE_OFFERED", "LINE_KYC_COMPLETED"],
                "key": "$topic",
                "minutes": 15,
            },
            {"id": "call-1", "type": "call", "template_id": "tpl-1"},
            {
                "id": "after-call-1",
                "type": "wait",
                "topics": ["call.completed"],
                "key": "outcome",
                "match": {"payload": "lead_id", "run": "lead_call-1"},
                "minutes": 1440,
            },
            {"id": "rest", "type": "wait", "minutes": 60},
        ],
        "edges": [
            ["quiet", "call-1", "timeout"],
            ["quiet", "rest", "else"],
            ["call-1", "after-call-1"],
            ["after-call-1", "rest", "else"],
        ],
        "goal": {"topics": ["LINE_ACTIVE"]},
        "priority": PRIORITY,
    }
    base.update(overrides)
    return base


DEFINITION = WorkflowDefinition.model_validate(_plan())


def _ist(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment on the plan's clock, October 2026."""
    return datetime(2026, 10, day, hour, minute, tzinfo=IST)


def _context(topic: str, at: datetime) -> Dict[str, Any]:
    return {
        "phone": "+919876543210",
        "latest_topic": topic,
        "latest_event_at": at.astimezone(timezone.utc).isoformat(),
    }


def _rank(topic: str, at: datetime, now: datetime) -> Optional[int]:
    rank = rank_for(DEFINITION, _context(topic, at), now)
    return rank["rank"] if rank else None


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def test_rank_for_live_kyc_offer() -> None:
    """The three ranks of the line plan, judged at 10:30 on 9 Oct."""
    now = _ist(9, 10, 30)
    live, kyc, offer = _ist(9, 10, 5), _ist(9, 2, 0), _ist(9, 6, 0)

    assert rank_for(DEFINITION, _context("LINE_KYC_COMPLETED", live), now) == {
        "rank": 1,
        "order": "first_ready",
        "event_ms": _ms(live),
    }
    assert rank_for(DEFINITION, _context("LINE_KYC_COMPLETED", kyc), now) == {
        "rank": 2,
        "order": "newest_event",
        "event_ms": _ms(kyc),
    }
    assert rank_for(DEFINITION, _context("LINE_OFFERED", offer), now) == {
        "rank": 3,
        "order": "newest_event",
        "event_ms": _ms(offer),
    }


def test_event_at_0930_is_pile() -> None:
    """Before the opening is not inside the window; nor is the closing minute."""
    now = _ist(9, 10, 30)

    assert _rank("LINE_OFFERED", _ist(9, 9, 30), now) == 3
    assert _rank("LINE_KYC_COMPLETED", _ist(9, 9, 30), now) == 2
    assert _rank("LINE_OFFERED", _ist(9, 20, 55), _ist(9, 20, 58)) == 1
    assert _rank("LINE_OFFERED", _ist(9, 20, 56), _ist(9, 20, 58)) == 3


def test_follow_up_keeps_the_persons_rank() -> None:
    """A later try is ranked by the person's stage event, not by when the try
    is queued: live stays live that day, pile stays pile, and yesterday's live
    person is pile today. A call queued at night finds no open window."""
    live_event, pile_event = _ist(9, 10, 7), _ist(9, 2, 0)

    assert _rank("LINE_OFFERED", live_event, _ist(9, 11, 5)) == 1
    assert _rank("LINE_OFFERED", live_event, _ist(9, 17, 5)) == 1
    assert _rank("LINE_KYC_COMPLETED", pile_event, _ist(9, 12, 0)) == 2
    assert _rank("LINE_OFFERED", live_event, _ist(10, 10, 30)) == 3
    assert _rank("LINE_OFFERED", _ist(8, 23, 10), _ist(8, 23, 25)) == 3


def test_a_run_older_than_the_stamps_is_read_from_its_founding_letter() -> None:
    founded = _ist(9, 10, 5)
    old_run = {"entered_event_at": founded.isoformat()}

    assert rank_for(DEFINITION, old_run, _ist(9, 10, 30)) == {
        "rank": 1,
        "order": "first_ready",
        "event_ms": _ms(founded),
    }
    assert rank_for(DEFINITION, {}, _ist(9, 10, 30)) == {
        "rank": 3,
        "order": "newest_event",
        "event_ms": 0,
    }


def test_a_rank_the_plan_does_not_list_is_newest_event() -> None:
    """Only the rank that differs is written: `"1": "first_ready"`. Any other
    rank a rule or the else names orders its calls newest event first."""
    short = {**PRIORITY, "ranks": {"1": "first_ready"}, "else": 9}
    definition = WorkflowDefinition.model_validate(_plan(priority=short))

    assert validate_definition(_plan(priority=short)) == []
    pile = rank_for(definition, _context("X", _ist(8, 23, 0)), _ist(9, 10, 30)) or {}
    assert (pile["rank"], pile["order"]) == (9, "newest_event")
    live = rank_for(definition, _context("X", _ist(9, 10, 5)), _ist(9, 10, 30)) or {}
    assert (live["rank"], live["order"]) == (1, "first_ready")


def test_publish_refuses_a_rule_on_a_fact_that_does_not_exist() -> None:
    """A misspelt field never holds, so every call would silently take the
    `else` rank."""
    rule = {
        "if": [{"field": "run.latest_topik", "op": "is", "value": "X"}],
        "rank": 1,
    }

    assert any(
        "run.latest_topik" in p
        for p in validate_definition(_plan(priority={**PRIORITY, "rules": [rule]}))
    )


@pytest.mark.parametrize(
    "field", ["run.latest_topic", "run.latest_event_at", "run.latest_event_today"]
)
def test_the_latest_facts_are_refused_outside_priority_rules(field: str) -> None:
    """They exist to rank a call; a condition square may not route on them."""
    plan = _plan()
    plan["nodes"].append(
        {
            "id": "gate",
            "type": "condition",
            "rules": [
                {"on": "yes", "if": [{"field": field, "op": "is", "value": "x"}]}
            ],
        }
    )
    plan["edges"] += [["gate", "rest", "yes"], ["gate", "rest", "else"]]

    assert any(field in p for p in validate_definition(plan))


def test_latest_keys_never_reach_the_payload() -> None:
    """Ours to write: dropped from a run's facts, and refused from a producer."""
    context = {**_context("LINE_OFFERED", _ist(9, 10, 5)), "customer_name": "Riya"}

    assert run_facts(context) == {"customer_name": "Riya"}
    assert _context_from_payload(
        {"latest_topic": "FORGED", "latest_event_at": "2020-01-01", "offer": "x"}, 256
    ) == {"offer": "x"}


def _run(context: Dict[str, Any]) -> EnrollmentRun:
    return EnrollmentRun(
        id="6f6603bf-1bf5-4f46-b242-58e9f40833d2",
        merchant_id="m1",
        workflow_id="11111111-1111-1111-1111-111111111111",
        workflow_version=1,
        customer_id="22222222-2222-2222-2222-222222222222",
        status="waiting",
        current_node="call-1",
        wake_at=None,
        entered_at="2026-10-09T04:00:00Z",
        exited_at=None,
        exit_reason=None,
        context=context,
        enrollment_key="k1",
        attempts=0,
        last_error=None,
    )


async def _minted(
    monkeypatch: pytest.MonkeyPatch, definition: WorkflowDefinition, run: EnrollmentRun
) -> Dict[str, Any]:
    """Run the call square at 10:30 on 9 Oct; return the lead it inserted."""
    minted: List[Dict[str, Any]] = []

    async def fake_create(**kw: Any) -> Any:
        minted.append(kw)
        return type("L", (), {"id": kw["id"]})()

    async def fake_template(_id: str) -> Any:
        return type(
            "T",
            (),
            {"id": "tpl-1", "name": "nudge", "reseller_id": "r1", "merchant_id": None},
        )()

    async def fake_config(_id: str) -> Any:
        return type("C", (), {"initial_offset": 0})()

    async def nothing(*_args: Any) -> None:
        return None

    monkeypatch.setattr(call_node, "create_lead_call_tracker", fake_create)
    monkeypatch.setattr(call_node, "get_lead_by_id", nothing)
    monkeypatch.setattr(call_node, "get_template_by_id", fake_template)
    monkeypatch.setattr(
        call_node, "get_call_execution_config_by_template_id", fake_config
    )
    monkeypatch.setattr(call_node, "update_lead_enrollment_id", nothing)
    monkeypatch.setattr(call_node, "_now", lambda: _ist(9, 10, 30))
    node = next(n for n in definition.nodes if n.id == "call-1")
    await call_node.execute(run, node, definition)
    return minted[0]


async def test_call_node_writes_priority_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rank rides the lead's meta_data, beside the run it belongs to; the
    stamps it was judged from stay out of the payload."""
    event_at = _ist(9, 2, 0)
    run = _run({**_context("LINE_KYC_COMPLETED", event_at), "offer": "5 lakh"})

    lead = await _minted(monkeypatch, DEFINITION, run)

    assert lead["meta_data"] == {
        "workflow_id": str(run.workflow_id),
        "enrollment_id": str(run.id),
        "priority": {"rank": 2, "order": "newest_event", "event_ms": _ms(event_at)},
    }
    assert lead["payload"] == {
        "offer": "5 lakh",
        "current_node": "call-1",
        "customer_mobile_number": "+919876543210",
    }


async def test_a_plan_with_no_priority_writes_the_meta_it_always_did(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = WorkflowDefinition.model_validate(_plan(priority=None))
    run = _run(_context("LINE_OFFERED", _ist(9, 10, 7)))

    lead = await _minted(monkeypatch, plain, run)

    assert lead["meta_data"] == {
        "workflow_id": str(run.workflow_id),
        "enrollment_id": str(run.id),
    }

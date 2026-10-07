"""A call's rank: decided by the plan's `priority` block, carried on the lead.

The call square works the rank out when it queues its lead and writes it on the
lead's meta_data, where the dialler reads it. A plan with no `priority` block is
exactly as it was.
"""

from datetime import datetime, time, timezone
from types import SimpleNamespace
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

    # A live call also says what it falls to if it is not called by closing.
    assert rank_for(DEFINITION, _context("LINE_KYC_COMPLETED", live), now) == {
        "rank": 1,
        "order": "first_ready",
        "event_ms": _ms(live),
        "next_rank": 2,
        "next_order": "newest_event",
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
        "next_rank": 3,  # no stamped topic: the plan's `else` tomorrow
        "next_order": "newest_event",
    }
    assert rank_for(DEFINITION, {}, _ist(9, 10, 30)) == {
        "rank": 3,
        "order": "newest_event",
        "event_ms": 0,
    }


NO_WINDOW = {k: v for k, v in PRIORITY.items() if k != "window"}
# The template's call hours, as its call_execution_config carries them (IST).
HOURS = SimpleNamespace(
    initial_offset=0, call_start_time=time(10, 0), call_end_time=time(20, 56)
)


def test_today_is_read_from_the_templates_call_hours() -> None:
    """A plan carries no call window: "today" is the template's call hours, on
    the dialler's clock (IST). With no hours at hand nobody is live. Publish
    does not ask for a window."""
    plan = WorkflowDefinition.model_validate(_plan(priority=NO_WINDOW))
    now = _ist(9, 10, 30)
    live = _context("LINE_KYC_COMPLETED", _ist(9, 10, 5))
    night = _context("LINE_KYC_COMPLETED", _ist(9, 9, 30))

    assert rank_for(plan, live, now, HOURS) == {
        "rank": 1,
        "order": "first_ready",
        "event_ms": _ms(_ist(9, 10, 5)),
        "next_rank": 2,
        "next_order": "newest_event",
    }
    assert (rank_for(plan, night, now, HOURS) or {})["rank"] == 2
    assert (rank_for(plan, live, now) or {})["rank"] == 2
    assert validate_definition(_plan(priority=NO_WINDOW)) == []


def test_a_window_the_plan_still_names_is_the_one_used() -> None:
    lunch = SimpleNamespace(call_start_time=time(12, 0), call_end_time=time(13, 0))

    assert _rank("LINE_OFFERED", _ist(9, 10, 5), _ist(9, 10, 30)) == 1
    live = _context("LINE_OFFERED", _ist(9, 10, 5))
    assert (rank_for(DEFINITION, live, _ist(9, 10, 30), lunch) or {})["rank"] == 1


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
    assert (live["next_rank"], live["next_order"]) == (9, "newest_event")


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


# Flipkart's block exactly as agreed (dialler plan section 4): no window, so
# "today" is the call template's hours.
FLIPKART: Dict[str, Any] = {
    "ranks": {"1": "first_ready", "2": "newest_event", "3": "newest_event"},
    "rules": [
        {
            "if": [
                {"field": "run.latest_event_today", "op": "is", "value": True},
                {
                    "field": "run.latest_topic",
                    "op": "in",
                    "value": ["LINE_OFFERED", "LINE_KYC_COMPLETED"],
                },
            ],
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


def test_flipkarts_block_publishes_and_ranks_live_kyc_offer() -> None:
    """Live KYC -> 1, KYC before today -> 2, an old offer -> 3, on the template's
    10:00-20:56 hours, judged at 10:30 on 9 Oct."""
    assert validate_definition(_plan(priority=FLIPKART)) == []
    definition = WorkflowDefinition.model_validate(_plan(priority=FLIPKART))
    hours = SimpleNamespace(call_start_time=time(10, 0), call_end_time=time(20, 56))
    now = _ist(9, 10, 30)

    def rank(topic: str, at: datetime) -> int:
        answer = rank_for(definition, _context(topic, at), now, hours)
        assert answer is not None
        return answer["rank"]

    assert rank("LINE_KYC_COMPLETED", _ist(9, 10, 5)) == 1
    assert rank("LINE_KYC_COMPLETED", _ist(8, 15, 0)) == 2
    assert rank("LINE_OFFERED", _ist(8, 15, 0)) == 3


@pytest.mark.parametrize(
    "condition",
    [
        {"field": "run.latest_event_today", "op": "is", "value": "true"},  # text
        {"field": "run.latest_event_today", "op": ">", "value": 1},
        {"field": "run.latest_topic", "op": "is", "value": 2},
        {"field": "run.latest_topic", "op": ">", "value": "LINE_OFFERED"},
        {"field": "run.latest_topic", "op": "in", "value": ["LINE_OFFERED", 3]},
        {"field": "run.latest_event_at", "op": "is", "value": "2026-10-09"},
        {"field": "run.latest_event_at", "op": ">", "value": "yesterday"},
        {"field": "run.latest_event_at", "op": ">", "value": "2026-10-09T10:00:00"},
    ],
)
def test_publish_refuses_a_rule_that_can_never_hold(condition: Dict[str, Any]) -> None:
    """An operator or a value that does not fit the fact never holds, so every
    call would silently take the `else` rank: "true" as text is not the
    boolean true."""
    rule = {"if": [condition], "rank": 1}

    assert any(
        condition["field"] in p
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
    monkeypatch: pytest.MonkeyPatch,
    definition: WorkflowDefinition,
    run: EnrollmentRun,
    config: Any = None,
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
        return config or type("C", (), {"initial_offset": 0})()

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


@pytest.mark.parametrize("key", ["01", "0", "100", "one"])
def test_publish_refuses_a_ranks_key_that_is_not_a_rank(key: str) -> None:
    """ "01" would publish and leave rank 1 silently on the default order."""
    priority = {**PRIORITY, "ranks": {key: "first_ready"}}

    assert any(repr(key) in p for p in validate_definition(_plan(priority=priority)))


@pytest.mark.parametrize(
    "condition",
    [
        {"field": "run.latest_topic", "op": "is", "value": "LINE_KYC_COMPLETD"},
        {
            "field": "run.latest_topic",
            "op": "in",
            "value": ["LINE_KYC_COMPLETED", "LINE_AGREEMENT_SIGNED"],
        },
    ],
)
def test_publish_refuses_a_topic_no_square_listens_for(
    condition: Dict[str, Any],
) -> None:
    """A topic the plan never hears is never stamped, so the rule never holds."""
    rule = {"if": [condition], "rank": 1}

    assert any(
        "no square in this workflow listens" in p
        for p in validate_definition(_plan(priority={**PRIORITY, "rules": [rule]}))
    )


def test_a_door_topic_may_be_named() -> None:
    rule = {
        "if": [{"field": "run.latest_topic", "op": "is", "value": "LINE_OFFERED"}],
        "rank": 1,
    }

    assert validate_definition(_plan(priority={**PRIORITY, "rules": [rule]})) == []


async def test_the_call_node_ranks_on_its_templates_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The square has the template's call config at hand: no window in the plan."""
    plan = WorkflowDefinition.model_validate(_plan(priority=NO_WINDOW))
    run = _run(_context("LINE_OFFERED", _ist(9, 10, 7)))

    lead = await _minted(monkeypatch, plan, run, HOURS)

    assert lead["meta_data"]["priority"]["rank"] == 1

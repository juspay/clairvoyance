"""The call square's lead id, and what makes it exactly-once.

A workflow call mints its lead id deterministically rather than keeping a
coordination table: the same visit computed twice is the same id, and the
primary key absorbs the duplicate. That property is the whole design, and
it had a hole — the id was keyed on (run, node), which is a property of the
SQUARE rather than of standing on it, so a run that came back round to the
same call square recomputed the identical id and its second call was
refused by the PK it was relying on.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import NAMESPACE_URL, uuid5

import pytest

import app.crm.outreach.nodes.call as call_node
from app.crm.outreach.ceiling import (
    CALLS_TODAY_KEY,
    calls_today,
    max_calls_reached,
    today_on,
)
from app.crm.outreach.db import UniqueViolation
from app.crm.outreach.nodes.call import MAX_CALLS_OUTCOME, execute
from app.crm.outreach.nodes.context import OUTCOME_KEY, is_bookkeeping, run_facts
from app.crm.outreach.schemas import (
    DEFAULT_MAX_CALLS_PER_DAY,
    DEFAULT_TIMEZONE,
    EnrollmentRun,
    WorkflowDefinition,
    WorkflowNode,
)

_NODE = WorkflowNode(id="nudge-call", type="call", template_id="tpl-1")
_DEFINITION = WorkflowDefinition(
    entry={"topic": "orders/create"},
    nodes=[_NODE],
    edges=[],
    goals=[{"topics": ["orders/paid"]}],
)


def _run(context: Optional[Dict[str, Any]] = None) -> EnrollmentRun:
    return EnrollmentRun(
        id="6f6603bf-1bf5-4f46-b242-58e9f40833d2",
        merchant_id="m1",
        workflow_id="11111111-1111-1111-1111-111111111111",
        workflow_version=1,
        customer_id="22222222-2222-2222-2222-222222222222",
        status="waiting",
        current_node="nudge-call",
        wake_at=None,
        entered_at="2026-09-09T11:26:00Z",
        exited_at=None,
        exit_reason=None,
        context={"phone": "+919110752252", **(context or {})},
        enrollment_key="k1",
        attempts=0,
        last_error=None,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    inserted: List[str],
    *,
    existing: Optional[set] = None,
) -> None:
    """The accessor as it really behaves: a duplicate primary key is caught
    broadly and returned as None, never as a UniqueViolation the caller can
    see. `existing` is the set of ids already in the table."""
    rows = existing if existing is not None else set()

    async def fake_get_lead(lead_id: str) -> Any:
        return type("L", (), {"id": lead_id})() if lead_id in rows else None

    async def fake_create(**kw: Any) -> Any:
        if kw["id"] in rows:
            return None  # the swallow — every failure looks like this
        rows.add(kw["id"])
        inserted.append(kw["id"])
        return type("L", (), {"id": kw["id"]})()

    async def fake_template(_id: str) -> Any:
        return type(
            "T",
            (),
            {"id": "tpl-1", "name": "nudge", "reseller_id": "r1", "merchant_id": None},
        )()

    async def fake_config(_id: str) -> Any:
        return type("C", (), {"initial_offset": 0})()

    async def fake_stamp(_lead_id: str, _run_id: str) -> None:
        return None

    monkeypatch.setattr(call_node, "create_lead_call_tracker", fake_create)
    monkeypatch.setattr(call_node, "get_lead_by_id", fake_get_lead)
    monkeypatch.setattr(call_node, "get_template_by_id", fake_template)
    monkeypatch.setattr(
        call_node, "get_call_execution_config_by_template_id", fake_config
    )
    monkeypatch.setattr(call_node, "update_lead_enrollment_id", fake_stamp)


def _expected(run_id: str, node_id: str, visit: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"crm-workflow-lead:{run_id}:{node_id}:{visit}"))


async def test_a_second_visit_to_the_same_square_gets_its_own_lead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A board that loops reaches the same call square twice on one run.

    Keyed on (run, node) both visits computed the same uuid5, the PK refused
    the second, the accessor returned None, and the square raised — a run
    parked on its second call.
    """
    inserted: List[str] = []
    _install(monkeypatch, inserted)

    first = await execute(_run(), _NODE, _DEFINITION)
    # The run carries the counter forward, exactly as the walker merges it.
    second = await execute(_run(first), _NODE, _DEFINITION)

    assert first["lead_nudge-call"] == _expected(str(_run().id), "nudge-call", 1)
    assert second["lead_nudge-call"] == _expected(str(_run().id), "nudge-call", 2)
    assert first["lead_nudge-call"] != second["lead_nudge-call"]
    assert len(inserted) == 2, "both visits must reach the lead table"


async def test_the_same_visit_run_twice_is_one_lead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease retry re-runs the SAME visit: the counter has not moved, the
    id recomputes identically, and the square adopts its own row instead of
    calling twice. Before this it raised on None and the run parked.
    """
    inserted: List[str] = []
    _install(monkeypatch, inserted)

    first = await execute(_run(), _NODE, _DEFINITION)
    # The crash: the patch was never merged, so the run still has no counter.
    retry = await execute(_run(), _NODE, _DEFINITION)

    assert retry["lead_nudge-call"] == first["lead_nudge-call"]
    assert len(inserted) == 1, "the retry must not place a second call"


async def test_a_unique_violation_that_escapes_the_accessor_is_absorbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accessor swallows a duplicate into None, so UniqueViolation never
    reaches this square — kept for the day it narrows, with the same outcome.
    """
    inserted: List[str] = []
    _install(monkeypatch, inserted)
    first = await execute(_run(), _NODE, _DEFINITION)

    async def raises_instead(**_kw: Any) -> Any:
        raise UniqueViolation("duplicate key value violates unique constraint")

    monkeypatch.setattr(call_node, "create_lead_call_tracker", raises_instead)
    retry = await execute(_run(), _NODE, _DEFINITION)

    assert retry["lead_nudge-call"] == first["lead_nudge-call"]
    assert len(inserted) == 1, "a violation must not place a second call"


async def test_a_genuine_insert_failure_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No row means the insert really failed, and the run parks."""
    inserted: List[str] = []
    _install(monkeypatch, inserted)

    async def always_none(**_kw: Any) -> None:
        return None

    monkeypatch.setattr(call_node, "create_lead_call_tracker", always_none)

    async def no_row(_lead_id: str) -> Any:
        return None

    monkeypatch.setattr(call_node, "get_lead_by_id", no_row)

    with pytest.raises(RuntimeError, match="lead insert returned None"):
        await execute(_run(), _NODE, _DEFINITION)


async def test_the_counter_never_reaches_a_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Our counting, not a fact about the customer. `lead_` is already a
    bookkeeping prefix, so run_facts filters it for free.
    """
    inserted: List[str] = []
    _install(monkeypatch, inserted)

    patch = await execute(_run(), _NODE, _DEFINITION)
    merged = {**_run().context, **patch}

    assert "lead_visits_nudge-call" in patch, "the counter is written"
    assert not any("visits" in key for key in run_facts(merged)), run_facts(merged)


async def test_a_run_from_before_the_counter_starts_at_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No counter, or an unreadable one, reads as 0 — so the next id is `:1`.
    A fresh lead is a duplicate call at worst; raising would park the run.
    """
    inserted: List[str] = []
    _install(monkeypatch, inserted)

    for junk in ({}, {"lead_visits_nudge-call": "two"}, {"lead_visits_nudge-call": -3}):
        assert call_node._visits_so_far(junk, "nudge-call") == 0

    patch = await execute(_run({"lead_visits_nudge-call": "junk"}), _NODE, _DEFINITION)
    assert patch["lead_nudge-call"] == _expected(str(_run().id), "nudge-call", 1)


# --- phase 20: the plan's DAILY ceiling --------------------------------------

IST = "Asia/Kolkata"
# A day the suite can never be running on, so "yesterday" is unambiguous.
_LONG_AGO = "2020-01-01"


def _capped(
    per_day: Optional[int],
    nodes: Optional[List[WorkflowNode]] = None,
    tz: str = IST,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        entry={"topic": "orders/create"},
        nodes=nodes or [_NODE],
        edges=[],
        goals=[{"topics": ["orders/paid"]}],
        exits={"max_calls_per_day": per_day, "timezone": tz},
    )


def _ledger(day: str, n: int) -> Dict[str, Any]:
    return {CALLS_TODAY_KEY: {"day": day, "n": n}}


def _install_untouchable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every accessor the square could reach, wired to fail the test: at the
    ceiling a visit must read and write nothing at all."""

    async def untouchable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a capped visit must read and write nothing")

    for name in (
        "create_lead_call_tracker",
        "get_lead_by_id",
        "get_template_by_id",
        "get_call_execution_config_by_template_id",
        "update_lead_enrollment_id",
    ):
        monkeypatch.setattr(call_node, name, untouchable)


async def test_at_todays_ceiling_the_square_places_no_call_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two placed today, a ceiling of two: no template read, no insert, no
    ledger write — only the fact and the trail word. And the same visit
    re-run under a lost lease says exactly the same thing."""
    _install_untouchable(monkeypatch)
    plan = _capped(2)
    run = _run(_ledger(today_on(plan.exits), 2))

    first = await execute(run, _NODE, plan)
    again = await execute(run, _NODE, plan)

    assert first == {OUTCOME_KEY: MAX_CALLS_OUTCOME}, "the trail word, and nothing else"
    assert again == first
    assert CALLS_TODAY_KEY not in first, "a capped visit never touches the ledger"
    assert "max_calls_reached" not in first, "the answer is computed, never stored"
    assert not any(key.startswith("lead_") for key in first)


async def test_a_new_day_resets_the_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE property. Yesterday's ledger is spent: the run dialled its fill on
    the 22nd and dials again on the 23rd, and the stale record is REPLACED,
    never added to."""
    inserted: List[str] = []
    _install(monkeypatch, inserted)
    plan = _capped(3)
    today = today_on(plan.exits)

    patch = await execute(_run(_ledger(_LONG_AGO, 3)), _NODE, plan)

    assert len(inserted) == 1, "yesterday's ceiling must not block today"
    assert patch[CALLS_TODAY_KEY] == {"day": today, "n": 1}
    assert not max_calls_reached(patch, plan.exits), "and one of three is not spent"


async def test_a_placed_call_counts_and_the_ledger_is_all_that_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One below the ceiling still mints and the ledger advances. The ledger
    is the WHOLE record of the ceiling: there is no second key saying whether
    it is reached, so there is nothing that can disagree with the count."""
    inserted: List[str] = []
    _install(monkeypatch, inserted)
    plan = _capped(2)
    today = today_on(plan.exits)

    patch = await execute(_run(_ledger(today, 1)), _NODE, plan)

    assert len(inserted) == 1
    assert patch[CALLS_TODAY_KEY] == {"day": today, "n": 2}
    assert "max_calls_reached" not in patch
    assert max_calls_reached(patch, plan.exits), "two of two: the next visit is capped"


async def test_the_visit_counters_are_never_reset_by_the_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scar this design exists around: lead ids are uuid5(run:node:visit),
    so the visit counter must stay monotonic for the run's LIFE. If a new day
    reset it, the first call of the day would re-derive an id the table
    already holds, be absorbed as a lease retry, and never be placed."""
    inserted: List[str] = []
    _install(monkeypatch, inserted)
    plan = _capped(3)
    # three visits yesterday, and the ledger is yesterday's
    context = {**_ledger(_LONG_AGO, 3), "lead_visits_nudge-call": 3}

    patch = await execute(_run(context), _NODE, plan)

    assert patch["lead_visits_nudge-call"] == 4, "the id counter keeps counting"
    assert patch[CALLS_TODAY_KEY]["n"] == 1, "the day ledger starts over"
    assert inserted == [_expected(str(_run().id), "nudge-call", 4)]


async def test_the_ceiling_counts_every_call_square_of_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ledger is the RUN'S, not the square's: calls from other call
    squares today count against the same ceiling."""
    others = [
        WorkflowNode(id="first-call", type="call", template_id="tpl-1"),
        WorkflowNode(id="second-call", type="call", template_id="tpl-1"),
    ]
    plan = _capped(4, others + [_NODE])
    today = today_on(plan.exits)

    _install_untouchable(monkeypatch)
    capped = await execute(_run(_ledger(today, 4)), _NODE, plan)
    assert capped == {OUTCOME_KEY: MAX_CALLS_OUTCOME}

    inserted: List[str] = []
    _install(monkeypatch, inserted)
    minted = await execute(_run(_ledger(today, 3)), _NODE, plan)
    assert len(inserted) == 1 and "lead_nudge-call" in minted


def test_the_day_is_read_on_the_plans_clock() -> None:
    """23:30 on the 22nd in Kolkata is still the 22nd — on the server's UTC
    clock it rolled to the 23rd two hours earlier, which would hand the run a
    fresh allowance at 18:30 local."""
    plan = _capped(3, tz=IST)
    late = datetime(2026, 9, 22, 18, 30, tzinfo=timezone.utc)  # 00:00 IST on the 23rd
    evening = datetime(
        2026, 9, 22, 17, 30, tzinfo=timezone.utc
    )  # 23:00 IST on the 22nd

    assert today_on(plan.exits, evening) == "2026-09-22"
    assert today_on(plan.exits, late) == "2026-09-23"
    # the same instants on a UTC plan are both still the 22nd
    utc_plan = _capped(3, tz="UTC")
    assert today_on(utc_plan.exits, evening) == "2026-09-22"
    assert today_on(utc_plan.exits, late) == "2026-09-22"


def test_calls_today_reads_junk_and_a_stale_day_as_zero() -> None:
    """A wrong ledger never parks a run over bookkeeping; it just does not
    count — the same rule _visits_so_far already had."""
    assert calls_today({}, "2026-09-22") == 0
    assert calls_today(_ledger("2026-09-21", 3), "2026-09-22") == 0
    assert calls_today({CALLS_TODAY_KEY: "three"}, "2026-09-22") == 0
    assert (
        calls_today({CALLS_TODAY_KEY: {"day": "2026-09-22", "n": -3}}, "2026-09-22")
        == 0
    )
    assert calls_today(_ledger("2026-09-22", 3), "2026-09-22") == 3


async def test_the_default_ceiling_binds_once_it_is_in_the_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every plan written today carries the default (plans.with_default_ceiling
    stamps it); the walker then enforces exactly what the document says."""
    assert DEFAULT_MAX_CALLS_PER_DAY == 6
    plan = _capped(DEFAULT_MAX_CALLS_PER_DAY, tz=DEFAULT_TIMEZONE)
    today = today_on(plan.exits)

    inserted: List[str] = []
    _install(monkeypatch, inserted)
    patch = await execute(_run(_ledger(today, 5)), _NODE, plan)
    assert len(inserted) == 1, "the sixth call of the day still goes"
    assert patch[CALLS_TODAY_KEY] == {"day": today, "n": 6}

    _install_untouchable(monkeypatch)
    capped = await execute(_run(_ledger(today, 6)), _NODE, plan)
    assert capped == {OUTCOME_KEY: MAX_CALLS_OUTCOME}, "the seventh does not"


async def test_taking_the_ceiling_away_frees_the_run_on_the_next_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run capped last night, republished with `max_calls_per_day: null`
    (+ on_publish migrate). Nothing has to be cleared, because nothing was
    stored: the predicate reads the LIVE ceiling, so the run is free the
    moment the document says so. A stored `true` would have needed a clear
    that only a PLACED call makes — and a capped run places none, which is
    the deadlock this design cannot have."""
    capped_plan = _capped(1)
    today = today_on(capped_plan.exits)
    context = {**_ledger(today, 1), "lead_visits_nudge-call": 3}
    assert max_calls_reached(context, capped_plan.exits), "capped under the old plan"

    uncapped = WorkflowDefinition(
        entry={"topic": "orders/create"},
        nodes=[_NODE],
        edges=[],
        goals=[{"topics": ["orders/paid"]}],
        exits={"max_calls_per_day": None},
    )
    assert not max_calls_reached(context, uncapped.exits), "free under the new one"

    inserted: List[str] = []
    _install(monkeypatch, inserted)
    patch = await execute(_run(context), _NODE, uncapped)
    assert len(inserted) == 1, "no ceiling: the call goes"
    assert CALLS_TODAY_KEY not in patch, "no ceiling, no ledger"


async def test_a_board_that_names_no_ceiling_grows_no_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the DOCUMENT says is what binds (ADR 0023 §1): a stored plan
    written before phase 20 has no ceiling and keeps dialling exactly as it
    did. The default is stamped into NEW documents at publish
    (plans.with_default_ceiling), never applied at read to an old one."""
    legacy = WorkflowDefinition(
        entry={"topic": "orders/create"},
        nodes=[_NODE],
        edges=[],
        goals=[{"topics": ["orders/paid"]}],
    )
    assert legacy.exits.max_calls_per_day is None, "no default is applied at read"

    inserted: List[str] = []
    _install(monkeypatch, inserted)
    patch = await execute(_run({"lead_visits_nudge-call": 40}), _NODE, legacy)
    assert len(inserted) == 1 and patch["lead_visits_nudge-call"] == 41
    assert CALLS_TODAY_KEY not in patch and "max_calls_reached" not in patch


def test_max_calls_reached_is_the_one_predicate_both_squares_ask() -> None:
    """The call square asks it before dialling and the condition square asks
    it to route. One implementation, so "did we place the last call?" and
    "should we route past the call?" can never answer differently."""
    plan = _capped(2)
    today = today_on(plan.exits)

    assert not max_calls_reached({}, plan.exits), "nothing placed"
    assert not max_calls_reached(_ledger(today, 1), plan.exits)
    assert max_calls_reached(_ledger(today, 2), plan.exits), "at it"
    assert max_calls_reached(_ledger(today, 9), plan.exits), "past it"
    assert not max_calls_reached(_ledger(_LONG_AGO, 9), plan.exits), "another day"
    # No ceiling is not "reached": an uncapped board routes down the else arm.
    assert not max_calls_reached(_ledger(today, 9), _capped(None).exits)


def test_the_ledger_is_ours_and_the_trail_word_never_reaches_a_template() -> None:
    """The two keys this phase puts in a run's context are both the walker's.
    The ledger because a producer who spelled it would hand the run a fresh
    allowance; the trail word because it belongs on the row, not in a
    message. run_facts drops both, so neither rides a lead payload."""
    assert is_bookkeeping(CALLS_TODAY_KEY), "ceiling.CALLS_TODAY_KEY is bookkeeping"
    assert is_bookkeeping(OUTCOME_KEY)
    facts = run_facts({CALLS_TODAY_KEY: {"day": "x", "n": 1}, OUTCOME_KEY: "max_calls"})
    assert CALLS_TODAY_KEY not in facts and OUTCOME_KEY not in facts


def test_the_bookkeeping_list_names_the_ledger_key_the_ceiling_owns() -> None:
    """`_BOOKKEEPING_KEYS` is a tuple of literals by design — it is read by
    eye as the list of what is ours. The ceiling owns the constant, so the
    two spellings are pinned here rather than left to drift: the day they
    disagree, a producer's `calls_today` is admitted and hands the run a
    fresh allowance."""
    from app.crm.outreach.nodes.context import _BOOKKEEPING_KEYS

    assert CALLS_TODAY_KEY in _BOOKKEEPING_KEYS


def test_a_producer_cannot_spell_the_answer_because_it_is_not_a_context_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer lives in the grammar's `run.` namespace, not the run's
    context, so there is nothing for a merchant's payload to collide with:
    `context.max_calls_reached` and `run.max_calls_reached` are two different
    fields, and only the engine can write the second. This is what replaced
    guarding a context key by name."""
    from app.crm.outreach import predicates

    plan = _capped(2)
    spent = _ledger(today_on(plan.exits), 2)
    # a merchant sends the word, with the opposite value, as an ordinary fact
    seeded = {**spent, "max_calls_reached": False}
    lens = predicates.RunLens(seeded, plan.exits)
    assert (
        predicates.lookup("context.max_calls_reached", seeded, {}, None, lens) is False
    )
    assert predicates.lookup("run.max_calls_reached", seeded, {}, None, lens) is True


async def test_the_condition_square_routes_on_the_computed_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the predicate: `run.max_calls_reached` is a
    field a condition may name even though the run's context never holds it.
    Same ledger, same plan, both squares agree."""
    from app.crm.outreach.nodes.condition import execute as condition_execute

    gate = WorkflowNode(
        id="was-it-capped",
        type="condition",
        rules=[
            {
                "on": "capped",
                "if": [{"field": "run.max_calls_reached", "op": "is", "value": True}],
            }
        ],
    )
    plan = WorkflowDefinition(
        entry={"topic": "orders/create"},
        nodes=[_NODE, gate],
        edges=[
            ["was-it-capped", "nudge-call", "capped"],
            ["was-it-capped", "nudge-call", "else"],
        ],
        goals=[{"topics": ["orders/paid"]}],
        exits={"max_calls_per_day": 2, "timezone": IST},
    )
    today = today_on(plan.exits)

    spent = await condition_execute(_run(_ledger(today, 2)), gate, plan)
    left = await condition_execute(_run(_ledger(today, 1)), gate, plan)

    assert spent == {"reply_was-it-capped": "capped"}
    assert left == {"reply_was-it-capped": "else"}

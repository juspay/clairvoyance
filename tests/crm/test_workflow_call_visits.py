"""The call square's lead id, and what makes it exactly-once.

A workflow call mints its lead id deterministically rather than keeping a
coordination table: the same visit computed twice is the same id, and the
primary key absorbs the duplicate. That property is the whole design, and
it had a hole — the id was keyed on (run, node), which is a property of the
SQUARE rather than of standing on it, so a run that came back round to the
same call square recomputed the identical id and its second call was
refused by the PK it was relying on.
"""

from typing import Any, Dict, List, Optional
from uuid import NAMESPACE_URL, uuid5

import pytest

import app.crm.outreach.nodes.call as call_node
from app.crm.outreach.db import UniqueViolation
from app.crm.outreach.nodes.call import execute
from app.crm.outreach.nodes.context import run_facts
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode

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

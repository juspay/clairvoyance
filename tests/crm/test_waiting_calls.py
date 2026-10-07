"""wake_waiting_calls: parked runs whose number no longer takes calls with no
lead row wake, and make their lead today's way."""

from datetime import datetime, timezone
from typing import List

import pytest

import app.crm.outreach.grant as grant_module
from app.crm.outreach.contracts import wake_waiting_calls
from app.crm.outreach.db.queries.grant import wake_runs_query
from tests.crm.test_call_parking import PLAN
from tests.crm.test_grant import _install, _lead_id, _World

WAKE = datetime(2026, 11, 1, tzinfo=timezone.utc)


async def test_only_a_run_still_parked_for_that_call_is_woken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dialler says a parked call's number no longer takes calls with no lead
    row: the run waiting on that call square for exactly that lead wakes (it makes
    the lead today's way). A run that moved on, or another lead id, is not touched."""
    w = _install(monkeypatch, _World(PLAN))
    parked = w.waiting(wake_at=WAKE)
    moved = w.waiting(wake_at=WAKE, current_node="after-call-1")
    woke: List[str] = []

    async def wake_runs(run_ids: List[str]) -> int:
        woke.extend(run_ids)
        return len(run_ids)

    monkeypatch.setattr(
        grant_module.grant_accessor, "wake_runs", wake_runs, raising=False
    )

    pairs = [
        (str(parked.id), _lead_id(parked)),
        (str(moved.id), _lead_id(moved)),
        (str(parked.id), "another-lead"),
    ]
    assert await wake_waiting_calls(pairs) == 1
    assert woke == [str(parked.id)]


def test_the_wake_leaves_a_run_already_due_or_leased_alone() -> None:
    sql, params = wake_runs_query(["r1"])
    assert "SET wake_at = now()" in sql and "wake_at > now()" in sql
    assert "status = 'waiting'" in sql and params == [["r1"]]

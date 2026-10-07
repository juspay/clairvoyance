"""wake_waiting_calls: parked runs whose number no longer takes calls with no
lead row wake, and make their lead today's way."""

from datetime import datetime, timezone
from typing import Any, List

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
    woke: List[Any] = []

    async def wake_runs(runs: List[Any]) -> int:
        woke.extend(runs)
        return len(runs)

    monkeypatch.setattr(
        grant_module.grant_accessor, "wake_runs", wake_runs, raising=False
    )

    pairs = [
        (str(parked.id), _lead_id(parked)),
        (str(moved.id), _lead_id(moved)),
        (str(parked.id), "another-lead"),
    ]
    assert await wake_waiting_calls(pairs) == 1
    # matched on the square read: a run a grant moved is left alone
    assert woke == [(str(parked.id), "call-1")]


def test_the_wake_matches_the_square_it_read() -> None:
    """Not the alarm: a stage letter's 1 ms nudge must not make it miss."""
    sql, params = wake_runs_query([("r1", "call-1")])
    assert "SET wake_at = now()" in sql and "r.at" not in sql
    assert "e.current_node = r.node" in sql and "e.status = 'waiting'" in sql
    assert params == [["r1"], ["call-1"]]

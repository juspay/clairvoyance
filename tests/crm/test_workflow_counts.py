"""Run-counting laws: one rule for what these numbers mean, at both
scales. The run_* read is swapped out, so the tally is pinned over
literal rows with no database in the loop."""

import asyncio
from typing import Any, Dict, List

import pytest

from app.crm.outreach import counts as counts_module
from app.crm.outreach.counts import (
    _tally,
    fold_counts,
    fold_counts_by_workflow,
)
from app.crm.outreach.db.queries import run_counts_all_query, run_counts_query
from app.crm.outreach.schemas import RunCounts


def _rows(rows: List[Dict[str, Any]]):
    """Stands in for the run_* read a fold calls."""

    async def _read(*_args: Any) -> List[Dict[str, Any]]:
        return rows

    return _read


def _fold(rows: List[Dict[str, Any]]) -> RunCounts:
    """The tally over a whole result set."""
    counts = RunCounts()
    for row in rows:
        _tally(counts, row)
    return counts


def test_run_counts_is_one_grouped_pass_over_this_merchants_flow() -> None:
    sql, params = run_counts_query("m1", "wf-1")
    assert "merchant_id = $1" in sql and "workflow_id = $2" in sql
    assert "GROUP BY status, exit_reason" in sql
    assert params == ["m1", "wf-1"]


def test_tally_reads_goal_met_as_a_reason_not_a_status() -> None:
    counts = _fold(
        [
            {"status": "waiting", "exit_reason": None, "n": 12},
            {"status": "parked", "exit_reason": None, "n": 3},
            {"status": "exited", "exit_reason": "goal_met", "n": 7},
            {"status": "exited", "exit_reason": "timed_out", "n": 2},
            {"status": "exited", "exit_reason": "completed", "n": 5},
        ]
    )
    assert (counts.waiting, counts.parked, counts.exited) == (12, 3, 14)
    # goal_met sits INSIDE exited — it is counted again, not instead.
    assert counts.goal_met == 7
    assert counts.total == 29


def test_tally_totals_a_status_it_has_never_heard_of() -> None:
    counts = _fold([{"status": "hibernating", "exit_reason": None, "n": 4}])
    assert counts.total == 4
    assert (counts.waiting, counts.parked, counts.exited) == (0, 0, 0)


def test_counts_of_a_flow_nobody_entered_are_all_zeroes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(counts_module, "run_counts", _rows([]))
    assert asyncio.run(fold_counts("m1", "wf-1")).model_dump() == {
        "total": 0,
        "waiting": 0,
        "parked": 0,
        "exited": 0,
        "goal_met": 0,
    }


def test_run_counts_all_groups_every_plan_in_one_pass() -> None:
    sql, params = run_counts_all_query("m1")
    assert "merchant_id = $1" in sql
    # workflow_id moves from the WHERE into the key — that IS the change.
    assert "workflow_id" not in sql.split("GROUP BY")[0].split("WHERE")[1]
    assert "GROUP BY workflow_id, status, exit_reason" in sql
    assert params == ["m1"]


def test_fold_by_workflow_splits_plans_and_tallies_each_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"workflow_id": "wf-1", "status": "waiting", "exit_reason": None, "n": 142},
        {"workflow_id": "wf-1", "status": "parked", "exit_reason": None, "n": 18},
        {"workflow_id": "wf-1", "status": "exited", "exit_reason": "goal_met", "n": 61},
        {
            "workflow_id": "wf-1",
            "status": "exited",
            "exit_reason": "timed_out",
            "n": 39,
        },
        {"workflow_id": "wf-2", "status": "waiting", "exit_reason": None, "n": 388},
    ]
    monkeypatch.setattr(counts_module, "run_counts_all", _rows(rows))
    by_plan = asyncio.run(fold_counts_by_workflow("m1"))

    assert set(by_plan) == {"wf-1", "wf-2"}
    assert (by_plan["wf-1"].waiting, by_plan["wf-1"].parked) == (142, 18)
    # goal_met is a REASON, so the rate is over the 100 that finished.
    assert (by_plan["wf-1"].goal_met, by_plan["wf-1"].exited) == (61, 100)
    assert by_plan["wf-2"].waiting == 388 and by_plan["wf-2"].parked == 0

    # One plan's slice must tally exactly as the per-flow read tallies it —
    # the list columns and the run header show the same three numbers.
    assert by_plan["wf-1"] == _fold(
        [row for row in rows if row["workflow_id"] == "wf-1"]
    )


def test_fold_by_workflow_omits_plans_nobody_entered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent rather than present with zeros — the caller reads absent as
    zero."""
    monkeypatch.setattr(counts_module, "run_counts_all", _rows([]))
    assert asyncio.run(fold_counts_by_workflow("m1")) == {}

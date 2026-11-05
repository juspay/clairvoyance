"""The console's Workflows tab, week-1 reads (1 Oct 2026).

Measured on the prod replica for flipkart-checkout-nudge-new (275k runs):
the Performance, Runs and Publish screens asked an ALL-TIME summary (6.4 s)
only to learn whether the plan had any runs / how many are open, the
Performance tab ran the run list and the 1.5 s per-run facts read twice
(/report and /calls/summary in parallel), the versions list counted open
runs once per version (2.3 s), and the Runs tab sent one page read per
version (32) for its filter counts. Every replacement here was checked to
return byte-identical data on the replica before it was written.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute

import app.crm.api as crm_api
import app.crm.outreach.api as outreach_api
from app.crm.auth import MERCHANT_SCOPE_MARK
from app.crm.outreach import analytics
from app.crm.outreach.db.accessors import enrollment as enrollment_accessor
from app.crm.outreach.db.decoders.enrollment import (
    decode_open_runs,
    decode_run_summary,
    decode_version_counts,
)
from app.crm.outreach.db.queries.enrollment import (
    open_by_status_query,
    runs_by_version_query,
    workflow_has_runs_query,
    workflow_split_counts_query,
    workflow_summary_query,
)
from app.crm.outreach.db.queries.version import list_versions_query
from app.crm.outreach.schemas import RunEnding, Workflow

T0 = datetime(2026, 9, 30, 18, 30, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 1, 18, 29, tzinfo=timezone.utc)


# --- the builders --------------------------------------------------------------


def test_has_runs_is_one_merchant_first_exists() -> None:
    sql, params = workflow_has_runs_query("m1", "wf-1")
    assert "SELECT EXISTS" in sql and "AS has_runs" in sql
    assert "merchant_id = $1 AND workflow_id = $2" in sql
    assert params == ["m1", "wf-1"]


def test_open_by_status_walks_only_open_runs() -> None:
    sql, params = open_by_status_query("m1", "wf-1")
    # spelled like open_by_node_query so the open-runs partial index serves it
    assert "status <> 'exited'" in sql and "GROUP BY status" in sql
    assert "merchant_id = $1 AND workflow_id = $2" in sql
    assert params == ["m1", "wf-1"]


def test_runs_by_version_counts_the_window_in_one_statement() -> None:
    sql, params = runs_by_version_query("m1", "wf-1", T0, T1)
    assert "GROUP BY workflow_version" in sql
    assert "entered_at >= $3::timestamptz" in sql
    assert "entered_at < $4::timestamptz" in sql
    assert params == ["m1", "wf-1", T0, T1]


def test_the_summary_sort_never_carries_the_context() -> None:
    """The context (goal amount) is reduced to one numeric column inside
    the fenced subquery, so percentile_cont sorts narrow rows."""
    sql, _ = workflow_summary_query("m1", "wf-1", T0, T1)
    inner, outer = sql.rsplit("OFFSET 0", 1)  # the clause, not the comment
    assert "context->'goal'->>'amount'" in inner
    assert "context" not in outer
    assert "ORDER BY minutes_to_exit" in sql


def test_the_split_counts_read_only_the_split_keys() -> None:
    sql, values = workflow_split_counts_query("m1", "wf-1", T0, T1)
    assert "jsonb_object_keys(e.context)" in sql
    assert "jsonb_each_text" not in sql
    assert "e.context ->> k AS arm" in sql and "k LIKE $5" in sql
    assert values[-1] == "split_%"


def test_the_versions_list_counts_open_runs_once() -> None:
    sql, params = list_versions_query("m1", "wf-1")
    assert "LEFT JOIN (" in sql and "GROUP BY e.workflow_version" in sql
    assert "COALESCE(o.open_runs, 0) AS open_runs" in sql
    # no correlated count per version row any more
    assert "e.workflow_version = v.version" not in sql
    assert params == ["m1", "wf-1"]


# --- the decoders --------------------------------------------------------------


def test_runs_by_version_decodes_to_ints_and_skips_null() -> None:
    rows = [
        {"workflow_version": 32, "runs": 13254},
        {"workflow_version": 31, "runs": 7},
        {"workflow_version": None, "runs": 3},
    ]
    assert decode_version_counts(rows) == {32: 13254, 31: 7}
    summary = decode_run_summary([], version_rows=rows)
    assert summary.runs_by_version == {32: 13254, 31: 7}
    assert decode_run_summary([]).runs_by_version == {}


def test_open_runs_always_name_both_open_statuses() -> None:
    out = decode_open_runs(
        [{"status": "waiting", "runs": 200084}],
        [{"current_node": "quiet-15m", "runs": 12}],
    )
    assert out.open == {"waiting": 200084, "parked": 0}
    assert out.open_by_node == {"quiet-15m": 12}


# --- the accessor: statements on the replica -----------------------------------


class _Replica:
    def __init__(self) -> None:
        self.queries: List[str] = []

    async def read(self, query: str, values: Any) -> List[Dict[str, Any]]:
        self.queries.append(query)
        if "AS has_runs" in query:
            return [{"has_runs": True}]
        return []


@pytest.mark.asyncio
async def test_a_windowed_summary_adds_the_version_counts(monkeypatch) -> None:
    replica = _Replica()
    monkeypatch.setattr(enrollment_accessor, "crm_replica_read", replica.read)
    await enrollment_accessor.workflow_summary("m1", "wf", T0, T1)
    assert len(replica.queries) == 5
    assert "GROUP BY workflow_version" in replica.queries[-1]


@pytest.mark.asyncio
async def test_an_all_time_summary_reads_exactly_what_it_did(monkeypatch) -> None:
    replica = _Replica()
    monkeypatch.setattr(enrollment_accessor, "crm_replica_read", replica.read)
    await enrollment_accessor.workflow_summary("m1", "wf", None, None)
    assert len(replica.queries) == 4
    assert not any("GROUP BY workflow_version" in q for q in replica.queries)


@pytest.mark.asyncio
async def test_open_runs_are_replica_reads(monkeypatch) -> None:
    replica = _Replica()
    monkeypatch.setattr(enrollment_accessor, "crm_replica_read", replica.read)
    open_runs = await enrollment_accessor.workflow_open_runs("m1", "wf")
    assert open_runs.open == {"waiting": 0, "parked": 0}
    assert len(replica.queries) == 2


@pytest.mark.asyncio
async def test_has_runs_reads_the_primary_never_the_replica(monkeypatch) -> None:
    """It rides the detail read the editor opens through: a busy or hung
    replica must not hold that up."""
    replica = _Replica()
    monkeypatch.setattr(enrollment_accessor, "crm_replica_read", replica.read)

    class _Primary:
        async def fetchrow(self, query: str, *values: Any) -> Dict[str, Any]:
            assert "AS has_runs" in query
            return {"has_runs": True}

    @asynccontextmanager
    async def primary():
        yield _Primary()

    monkeypatch.setattr(enrollment_accessor, "crm_connection", primary)
    assert await enrollment_accessor.workflow_has_runs("m1", "wf") is True
    assert replica.queries == []


# --- the report folds the calls summary from the same reads -------------------

_RUN = str(uuid4())
_ENDINGS = [
    RunEnding(
        id=_RUN,
        enrollment_key="k1",
        status="exited",
        exit_reason="goal_met",
        exited_at=T1,
        entered_at=T0,
        current_node=None,
    )
]
_FACTS = {
    _RUN: [
        {
            "template": "nudge",
            "leads": 2,
            "placed_finished": 2,
            "placed": 2,
            "answered": 1,
            "no_answer": 1,
            "busy": 0,
            "in_progress": 0,
            "first_answered_at": T0,
            "last_answered_at": T0,
            "last_answered_event": None,
            "outcomes": {"NO_ANSWER": 1, "INTERESTED": 1},
        }
    ]
}
_STATS = [
    {
        "template": "nudge",
        "outcome": "NO_ANSWER",
        "spoke": False,
        "calls": 1,
        "runs": 1,
        "talk_seconds": None,
        "timed_calls": 0,
        "attempts": 1,
        "cost": 0.5,
    },
    {
        "template": "nudge",
        "outcome": "INTERESTED",
        "spoke": True,
        "calls": 1,
        "runs": 1,
        "talk_seconds": 42.0,
        "timed_calls": 1,
        "attempts": 1,
        "cost": 1.5,
    },
]


def _fake_reads(monkeypatch) -> Dict[str, int]:
    calls = {"endings": 0, "facts": 0, "stats": 0}

    async def plan(merchant_id: str, workflow_id: str) -> object:
        return object()

    async def endings(*_a: Any) -> List[RunEnding]:
        calls["endings"] += 1
        return _ENDINGS

    async def facts(merchant_id: str, runs: List[Tuple[str, Any, Any]]) -> Dict:
        calls["facts"] += 1
        return _FACTS

    async def stats(merchant_id: str, runs: List[Tuple[str, Any, Any]]) -> List:
        calls["stats"] += 1
        return _STATS

    async def no_cap(merchant_id: str) -> None:
        return None

    monkeypatch.setattr(analytics.workflow_accessor, "get_workflow", plan)
    monkeypatch.setattr(analytics.enrollment_accessor, "run_endings_in_window", endings)
    monkeypatch.setattr(analytics, "get_call_facts_by_runs", facts)
    monkeypatch.setattr(analytics, "get_call_stats_by_runs", stats)
    monkeypatch.setattr(analytics, "_merchant_max_calls", no_cap)
    return calls


@pytest.mark.asyncio
async def test_include_calls_runs_the_run_list_and_facts_once(monkeypatch) -> None:
    calls = _fake_reads(monkeypatch)
    report = await analytics.workflow_report("m1", "wf", T0, T1, include_calls=True)
    assert report is not None and report.calls_summary is not None
    assert calls == {"endings": 1, "facts": 1, "stats": 1}


@pytest.mark.asyncio
async def test_the_folded_calls_summary_is_the_calls_summary(monkeypatch) -> None:
    _fake_reads(monkeypatch)
    report = await analytics.workflow_report("m1", "wf", T0, T1, include_calls=True)
    alone = await analytics.workflow_call_summary("m1", "wf", T0, T1)
    assert report is not None
    assert report.calls_summary == alone
    assert alone.contacted_runs == 1 and alone.reached_runs == 1


@pytest.mark.asyncio
async def test_a_report_without_include_calls_is_unchanged(monkeypatch) -> None:
    calls = _fake_reads(monkeypatch)
    report = await analytics.workflow_report("m1", "wf", T0, T1)
    assert report is not None and report.calls_summary is None
    assert calls == {"endings": 1, "facts": 1, "stats": 0}


# --- the routes ----------------------------------------------------------------


def _workflow() -> Workflow:
    return Workflow(
        id=uuid4(),
        merchant_id="m1",
        name="plan",
        status="live",
        version=1,
        created_by=None,
        created_at=T0,
        updated_at=T0,
        definition={},
        draft=None,
    )


@pytest.mark.asyncio
async def test_the_detail_read_carries_has_runs(monkeypatch) -> None:
    async def get_workflow(merchant_id: str, workflow_id: str) -> Workflow:
        return _workflow()

    async def has_runs(merchant_id: str, workflow_id: str) -> bool:
        return True

    monkeypatch.setattr(outreach_api.plans, "get_workflow", get_workflow)
    monkeypatch.setattr(outreach_api.analytics, "workflow_has_runs", has_runs)
    out = await outreach_api.get_workflow_route("wf", merchant_id="m1")
    assert out.has_runs is True


@pytest.mark.asyncio
async def test_a_replica_failure_never_breaks_opening_a_workflow(monkeypatch) -> None:
    async def get_workflow(merchant_id: str, workflow_id: str) -> Workflow:
        return _workflow()

    async def broken(merchant_id: str, workflow_id: str) -> Optional[bool]:
        raise RuntimeError("replica down")

    monkeypatch.setattr(outreach_api.plans, "get_workflow", get_workflow)
    monkeypatch.setattr(outreach_api.analytics, "workflow_has_runs", broken)
    out = await outreach_api.get_workflow_route("wf", merchant_id="m1")
    assert out.has_runs is None  # the console falls back to the summary


def test_the_open_runs_route_is_merchant_scoped_and_mounted() -> None:
    matches = [
        r
        for r in outreach_api.router.routes
        if isinstance(r, APIRoute) and r.path == "/{workflow_id}/open"
    ]
    assert len(matches) == 1 and matches[0].methods == {"GET"}
    assert any(
        getattr(dep.call, MERCHANT_SCOPE_MARK, False)
        for dep in matches[0].dependant.dependencies
    )
    mounted = [r.path for r in crm_api.router.routes if isinstance(r, APIRoute)]
    assert "/workflows/{workflow_id}/open" in mounted

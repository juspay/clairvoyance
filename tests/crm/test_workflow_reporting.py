"""Runs reporting (rollout phase 09, G9): the per-plan summary and the
customer's journey across plans — the read the clocks pattern (§13) owed.

The summary is ONE statement (grouping sets) folded by a pure decoder;
the journey read joins the plan's name onto the customer's runs; the
goal-cancel stashes the goal event on the run so recovered revenue can be
summed later. Route wiring is checked by inspecting the routers, so a
route that lost its admin dependency or its mount fails here."""

import inspect
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from uuid import uuid4

from fastapi.routing import APIRoute

import app.crm.api as crm_api
import app.crm.outreach.api as outreach_api
from app.crm.auth import crm_admin_user
from app.crm.outreach.db.decoders.enrollment import (
    decode_customer_run,
    decode_run_summary,
)
from app.crm.outreach.db.queries.enrollment import (
    cancel_run_query,
    customer_runs_query,
    workflow_summary_query,
)
from app.crm.outreach.nodes import run_facts

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
SINCE, UNTIL = NOW - timedelta(days=7), NOW


# --- the builders ---


def test_summary_is_one_merchant_first_statement_with_a_window() -> None:
    sql, params = workflow_summary_query("m1", "wf-1", SINCE, UNTIL)
    assert "merchant_id = $1 AND workflow_id = $2" in sql
    assert (
        "entered_at >= $3::timestamptz" in sql and "entered_at < $4::timestamptz" in sql
    )
    assert "$3::timestamptz IS NULL OR" in sql and "$4::timestamptz IS NULL OR" in sql
    assert "GROUP BY GROUPING SETS ((status, exit_reason), ())" in sql
    assert "GROUPING(status, exit_reason)" in sql
    assert "percentile_cont(0.5)" in sql and "exited_at - entered_at" in sql
    assert "context->'goal'->>'amount'" in sql and "exit_reason = 'goal_met'" in sql
    assert params == ["m1", "wf-1", SINCE, UNTIL]
    _, params = workflow_summary_query("m1", "wf-1", None, None)
    assert params == ["m1", "wf-1", None, None]  # no window = all time


def test_customer_runs_join_the_plan_name_in_entry_order() -> None:
    sql, params = customer_runs_query("m1", "c-1", 100)
    assert "e.merchant_id = $1 AND e.customer_id = $2" in sql
    assert "JOIN crm_workflow w" in sql and "w.name AS workflow_name" in sql
    assert "w.merchant_id = e.merchant_id AND w.id = e.workflow_id" in sql
    assert "ORDER BY e.entered_at, e.id" in sql and "LIMIT $3" in sql
    assert params == ["m1", "c-1", 100]


def test_goal_cancel_can_stash_the_goal_on_the_runs_it_ends() -> None:
    """The patch rides the same UPDATE: context = context || $n::jsonb.
    Its placeholder follows the optional key, so both shapes are pinned."""
    patch = {
        "goal": {"topic": "orders/create", "event_id": "ev-1", "amount": "1850.00"}
    }
    sql, params = cancel_run_query(
        "m1", "run-1", "goal_met", NOW, key=("cart_token", "chk-1"), context_patch=patch
    )
    assert "context = context || $7::jsonb" in sql and "AND context->>$5 = $6" in sql
    assert len(params) == 7 and '"1850.00"' in params[6]
    sql, params = cancel_run_query(
        "m1", "run-1", "converted_elsewhere", NOW, context_patch=patch
    )
    assert "context = context || $5::jsonb" in sql and "$6" not in sql
    assert len(params) == 5
    sql, params = cancel_run_query("m1", "run-1", "goal_met", NOW)
    assert "context = context ||" not in sql and len(params) == 4


def test_the_goal_stash_is_bookkeeping_not_a_template_variable() -> None:
    facts = run_facts({"cart_token": "chk-1", "goal": {"topic": "orders/create"}})
    assert facts == {"cart_token": "chk-1"}


# --- the decoder folds grouping-set rows into one summary ---


def _row(
    status: Any,
    reason: Any,
    level: int,
    runs: int,
    median: Any = None,
    amount: Any = None,
) -> Dict[str, Any]:
    return {
        "status": status,
        "exit_reason": reason,
        "grouping_level": level,
        "runs": runs,
        "median_minutes_to_exit": median,
        "recovered_amount": amount,
    }


def test_decoder_folds_the_grouping_rows() -> None:
    rows = [
        _row("waiting", None, 0, 4),
        _row("parked", None, 0, 1),
        _row("exited", "goal_met", 0, 6, 42.0, 5400),
        _row("exited", "converted_elsewhere", 0, 2, 30.0, None),
        _row("exited", "completed", 0, 3, 1500.0, None),
        _row(None, None, 3, 16, 61.5, 5400),  # the () set: the whole window
    ]
    summary = decode_run_summary(rows)
    assert summary.runs == 16
    assert summary.open == {"waiting": 4, "parked": 1}
    assert summary.by_exit_reason == {
        "goal_met": 6,
        "converted_elsewhere": 2,
        "completed": 3,
    }
    assert summary.median_minutes_to_exit == 61.5
    assert summary.recovered_amount == 5400.0


def test_decoder_is_total_on_an_empty_window() -> None:
    summary = decode_run_summary([])
    assert summary.runs == 0 and summary.open == {"waiting": 0, "parked": 0}
    assert summary.by_exit_reason == {} and summary.median_minutes_to_exit is None
    assert summary.recovered_amount is None


def test_decoder_carries_the_plan_name_on_a_customer_run() -> None:
    run_id, wf_id, cust_id = uuid4(), uuid4(), uuid4()
    row = {
        "id": run_id,
        "merchant_id": "m1",
        "workflow_id": wf_id,
        "workflow_version": 1,
        "customer_id": cust_id,
        "status": "exited",
        "current_node": "wait-30m",
        "wake_at": None,
        "entered_at": NOW,
        "exited_at": NOW,
        "exit_reason": "goal_met",
        "context": '{"application_id": "APP-1"}',
        "enrollment_key": "APP-1",
        "attempts": 1,
        "last_error": None,
        "workflow_name": "loan-dropoff-02-kyc",
    }
    run = decode_customer_run(row)
    assert run.workflow_name == "loan-dropoff-02-kyc" and run.context == {
        "application_id": "APP-1"
    }


# --- the doors ---


def _route(router: Any, path: str) -> APIRoute:
    matches = [r for r in router.routes if isinstance(r, APIRoute) and r.path == path]
    assert len(matches) == 1, path
    return matches[0]


def test_summary_and_journey_routes_are_admin_only_and_mounted() -> None:
    summary = _route(outreach_api.router, "/{workflow_id}/summary")
    journey = _route(outreach_api.customer_router, "/{customer_id}/runs")
    for route in (summary, journey):
        assert route.methods == {"GET"}
        guard = inspect.signature(route.endpoint).parameters["current_user"].default
        assert guard.dependency is crm_admin_user
    mounted = [r.path for r in crm_api.router.routes if isinstance(r, APIRoute)]
    assert "/workflows/{workflow_id}/summary" in mounted
    assert "/customers/{customer_id}/runs" in mounted

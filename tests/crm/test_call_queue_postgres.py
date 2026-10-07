"""The waiting call's statements on a real Postgres: the hold, the re-arm, the
wakes and the grant's read. Temp tables shaped like the real ones."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.crm.outreach.db.queries.enrollment import (
    rearm_after_nudge_query,
    wake_plan_parked_calls_query,
)
from app.crm.outreach.db.queries.grant import (
    get_runs_query,
    parked_call_squares_query,
    wake_parked_calls_batch_query,
    wake_runs_query,
)
from tests.crm.conftest import CRM_WEBHOOK_TEST_DSN as DSN

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run against Postgres"
    ),
]

HOLD = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)
MAX_AGE = HOLD + timedelta(days=7)
CALL = {"id": "call-1", "type": "call", "topics": ["LINE_KYC_COMPLETED"]}


async def _tables(conn: Any) -> None:
    await conn.execute(
        "CREATE TEMP TABLE crm_workflow (merchant_id text, id uuid, status text,"
        " definition jsonb)"
    )
    await conn.execute(
        "CREATE TEMP TABLE crm_workflow_version (merchant_id text, workflow_id uuid,"
        " version int, definition jsonb)"
    )
    await conn.execute(
        "CREATE TEMP TABLE crm_workflow_enrollment (id uuid, merchant_id text,"
        " workflow_id uuid, workflow_version int, customer_id uuid, status text,"
        " current_node text, wake_at timestamptz, entered_at timestamptz,"
        " exited_at timestamptz, exit_reason text, context jsonb,"
        " enrollment_key text, attempts int, last_error text,"
        " node_arrived_at timestamptz)"
    )


async def _plan(conn: Any, status: str, *versions: Any) -> UUID:
    """A plan whose version n (from 1) has the nodes versions[n-1]; the live
    document is the last version."""
    plan, versions = uuid4(), versions or ((CALL,),)
    for number, nodes in enumerate(versions, start=1):
        await conn.execute(
            "INSERT INTO crm_workflow_version VALUES ('m1', $1, $2, $3::jsonb)",
            plan,
            number,
            json.dumps({"nodes": list(nodes)}),
        )
    await conn.execute(
        "INSERT INTO crm_workflow VALUES ('m1', $1, $2, $3::jsonb)",
        plan,
        status,
        json.dumps({"nodes": list(versions[-1])}),
    )
    return plan


async def _run(
    conn: Any, plan: UUID, node: str, wake: datetime, version: int = 1
) -> UUID:
    run = uuid4()
    await conn.execute(
        "INSERT INTO crm_workflow_enrollment VALUES ($1, 'm1', $2, $6, $3, 'waiting',"
        " $4, $5, now(), NULL, NULL, '{}'::jsonb, 'k', 0, NULL, NULL)",
        run,
        plan,
        uuid4(),
        node,
        wake,
        version,
    )
    return run


async def _wake(conn: Any, run: UUID) -> datetime:
    return await conn.fetchval(
        "SELECT wake_at FROM crm_workflow_enrollment WHERE id = $1", run
    )


async def _one(conn: Any, query: Any) -> Any:
    sql, params = query
    return await conn.fetch(sql, *params)


async def test_a_nudged_lease_is_rearmed_and_a_letters_wake_is_left() -> None:
    conn = await asyncpg.connect(DSN)
    try:
        await _tables(conn)
        plan = await _plan(conn, "live")
        nudged = await _run(conn, plan, "quiet", HOLD + timedelta(milliseconds=2))
        resumed = await _run(conn, plan, "quiet", HOLD - timedelta(minutes=4))
        for run in (nudged, resumed):
            await _one(conn, rearm_after_nudge_query(str(run), "quiet", HOLD))
        assert await _wake(conn, nudged) < HOLD
        assert await _wake(conn, resumed) == HOLD - timedelta(minutes=4)
    finally:
        await conn.close()


async def test_the_wake_needs_the_square_it_read() -> None:
    conn = await asyncpg.connect(DSN)
    try:
        await _tables(conn)
        plan = await _plan(conn, "live")
        parked = await _run(conn, plan, "call-1", MAX_AGE)
        moved = await _run(conn, plan, "after-call-1", MAX_AGE)
        rows = await _one(
            conn,
            wake_runs_query(
                [
                    (str(parked), "call-1"),
                    (str(moved), "call-1"),
                ]
            ),
        )
        assert [r["id"] for r in rows] == [parked]
    finally:
        await conn.close()


async def test_the_grants_read_carries_the_plans_status() -> None:
    conn = await asyncpg.connect(DSN)
    try:
        await _tables(conn)
        run = await _run(conn, await _plan(conn, "paused"), "call-1", MAX_AGE)
        (row,) = await _one(conn, get_runs_query([str(run)]))
        assert row["id"] == run and row["plan_status"] == "paused"
    finally:
        await conn.close()


async def _heal(conn: Any, batch: int) -> list:
    """The accessor's batch loop, on this connection."""
    rows = await _one(conn, parked_call_squares_query())
    squares = [
        (r["merchant_id"], r["workflow_id"], r["version"], r["node"]) for r in rows
    ]
    woken, after = [], "00000000-0000-0000-0000-000000000000"
    while squares:
        ids = sorted(
            str(r["id"])
            for r in await _one(
                conn, wake_parked_calls_batch_query(squares, after, batch)
            )
        )
        woken += ids
        if len(ids) < batch:
            break
        after = ids[-1]
    return woken


async def test_after_a_loss_only_live_parked_calls_wake_in_batches() -> None:
    conn = await asyncpg.connect(DSN)
    try:
        await _tables(conn)
        live, paused = await _plan(conn, "live"), await _plan(conn, "paused")
        plain = await _plan(conn, "live", [{"id": "call-1", "type": "call"}])
        woken = {str(await _run(conn, live, "call-1", MAX_AGE)) for _ in range(5)}
        left: Dict[str, UUID] = {
            "paused plan": await _run(conn, paused, "call-1", MAX_AGE),
            "no topics": await _run(conn, plain, "call-1", MAX_AGE),
            "another square": await _run(conn, live, "after-call-1", MAX_AGE),
        }
        assert sorted(await _heal(conn, batch=2)) == sorted(woken)
        for why, run in left.items():
            assert await _wake(conn, run) == MAX_AGE, why
    finally:
        await conn.close()


async def test_resume_wakes_runs_by_their_pinned_version() -> None:
    """v1 named the square `call-old`; v2 renamed it. Runs on each version wake
    on their own version's square; a v1 run on a v2-only name does not."""
    conn = await asyncpg.connect(DSN)
    try:
        await _tables(conn)
        old = [{**CALL, "id": "call-old"}]
        plan = await _plan(conn, "live", old, (CALL,))
        on_v1 = await _run(conn, plan, "call-old", MAX_AGE, version=1)
        on_v2 = await _run(conn, plan, "call-1", MAX_AGE, version=2)
        stray = await _run(conn, plan, "call-1", MAX_AGE, version=1)
        rows = await _one(conn, wake_plan_parked_calls_query("m1", str(plan)))
        assert {r["id"] for r in rows} == {on_v1, on_v2}
        assert await _wake(conn, stray) == MAX_AGE
    finally:
        await conn.close()

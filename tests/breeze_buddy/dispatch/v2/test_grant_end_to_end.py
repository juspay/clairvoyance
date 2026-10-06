"""The lead row is made only when a line is granted: the CRM side and the dialler side
joined, with nothing faked. The real walker parks a run on a call node, the real hooks
queue its id in Redis, the real Lua grants a line, and the real grant worker asks the real
``materialize_call`` on a real Postgres.

Skips unless both stores are given. The database is a throwaway one that carries every
migration: the walker claims whatever run is due in it.

    BB_TEST_PG_DSN=postgresql:///v2_full_test BB_TEST_REDIS_URL=redis://localhost:56409/15 \\
        uv run pytest tests/breeze_buddy/dispatch/v2/test_grant_end_to_end.py
"""

from __future__ import annotations

import copy
import json
import math
import os
import uuid
from types import SimpleNamespace

import asyncpg
import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
import app.database as database
from app.ai.voice.agents.breeze_buddy.dispatch import queue
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import grants, intents, scripts
from app.core import call_queue
from app.crm.outreach import walker
from app.crm.outreach.db.accessors import enrollment as enrollment_accessor
from tests.breeze_buddy.dispatch.v2.conftest import seed_number
from tests.crm.test_call_parking import PLAN

DSN = os.environ.get("BB_TEST_PG_DSN")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DSN, reason="set BB_TEST_PG_DSN to run the joined test"),
]

BAND = 10**13


@pytest.fixture
async def w(rr, monkeypatch):
    """One run due on the call node ``call-1`` of a live plan, and number N1 (no line
    yet) carrying the node's template. This test's own rows are removed after, except
    the plan and its version: a version row can never be deleted (migration 067)."""

    async def seen():
        return True

    for module in (grants, intents, queue):
        monkeypatch.setattr(module, "v2_seen", seen)
    monkeypatch.setattr(
        call_queue, "_hooks", (intents.queue, intents.withdraw, intents.rerank)
    )
    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, database.pool = database.pool, pool  # type: ignore[assignment]
    conn = await asyncpg.connect(DSN)
    merchant = f"e2e-{uuid.uuid4().hex[:8]}"
    try:
        template = str(
            await conn.fetchval(
                "INSERT INTO template (name, merchant_id, reseller_id)"
                " VALUES ($1, $1, 'R1') RETURNING id",
                merchant,
            )
        )
        await conn.execute(
            "INSERT INTO call_execution_config (id, initial_offset, retry_offset,"
            " call_start_time, call_end_time, max_retry, calling_provider, template,"
            " template_id, merchant_id, reseller_id)"
            " VALUES ($1, 0, 60, '00:00', '23:59:59', 1, 'PLIVO', $1, $2, $1, 'R1')",
            merchant,
            uuid.UUID(template),
        )
        plan = copy.deepcopy(PLAN)
        next(n for n in plan["nodes"] if n["id"] == "call-1")["template_id"] = template
        workflow = await conn.fetchval(
            "INSERT INTO crm_workflow (merchant_id, name, definition, status, version)"
            " VALUES ($1, $1, $2::jsonb, 'live', 1) RETURNING id",
            merchant,
            json.dumps(plan),
        )
        await conn.execute(
            "INSERT INTO crm_workflow_version (merchant_id, workflow_id, version,"
            " definition) VALUES ($1, $2, 1, $3::jsonb)",
            merchant,
            workflow,
            json.dumps(plan),
        )
        run = str(
            await conn.fetchval(
                "INSERT INTO crm_workflow_enrollment (merchant_id, workflow_id,"
                " workflow_version, customer_id, current_node, wake_at, context,"
                " enrollment_key) VALUES ($1, $2, 1, $3, 'call-1', now(), $4::jsonb, $1)"
                " RETURNING id",
                merchant,
                workflow,
                uuid.uuid4(),
                json.dumps({"phone": "+919876543210"}),
            )
        )
        await seed_number(rr, "N1", 0, {template: {}})
        await rr.hset("bb:num:N1", mapping={"intents": "1", "ranked": "1"})
        yield SimpleNamespace(
            rr=rr, conn=conn, merchant=merchant, template=template, run=run
        )
    finally:
        for table in (
            "lead_call_tracker",
            "crm_workflow_step",
            "crm_workflow_enrollment",
            "call_execution_config",
            "template",
        ):
            await conn.execute(f"DELETE FROM {table} WHERE merchant_id = $1", merchant)
        await conn.close()
        database.pool = previous
        await pool.close()


async def walk(w) -> None:
    """The real walker: claim what is due and walk this test's run."""
    for run in await walker.claim_due_runs(50):
        if str(run.id) == w.run:
            await walker.walk_run(run)


async def node(w) -> str:
    return await w.conn.fetchval(
        "SELECT current_node FROM crm_workflow_enrollment WHERE id = $1",
        uuid.UUID(w.run),
    )


async def leads(w) -> list:
    return await w.conn.fetch(
        "SELECT id, status, enrollment_id, meta_data, next_attempt_at <= now() AS due"
        " FROM lead_call_tracker WHERE merchant_id = $1",
        w.merchant,
    )


async def parked_then_granted(w) -> str:
    """The run parks and its id waits in the room with its rank; then a line opens and
    ``match`` reserves it. Returns the queued id. No lead row exists at any point."""
    await walk(w)
    ((lead_id, score),) = await w.rr.zrange(
        f"bb:q:{w.template}", 0, -1, withscores=True
    )
    assert await w.rr.hget(f"bb:qi:{w.template}", lead_id) == w.run
    assert math.floor(score / BAND) + 100 == 3  # no letter today: the plan's `else`
    assert await node(w) == "call-1" and await leads(w) == []

    await w.rr.hset("bb:num:N1", "max", 1)
    await scripts.match("N1")
    (entry,) = await w.rr.lrange("bb:grants", 0, -1)
    assert entry.startswith(f"N1|{lead_id}|") and entry.endswith(f"|{w.run}")
    assert await w.rr.llen("bb:tickets") == 0 and await leads(w) == []
    return lead_id


async def test_a_granted_line_makes_the_lead_and_moves_the_run(w):
    lead_id = await parked_then_granted(w)

    await grants.GrantWorker()._round()

    (lead,) = await leads(w)
    assert (lead["id"], lead["status"], lead["due"]) == (lead_id, "BACKLOG", True)
    assert str(lead["enrollment_id"]) == w.run
    assert json.loads(lead["meta_data"])["priority"]["rank"] == 3
    assert await node(w) == "after-call-1"
    (ticket,) = await w.rr.lrange("bb:tickets", 0, -1)
    assert ticket.startswith(f"N1|{lead_id}|")
    assert "g" not in json.loads(await w.rr.hget("bb:inflight:N1", lead_id))


@pytest.mark.parametrize("walked", [True, False])
async def test_an_event_before_the_grant_leaves_no_lead_and_frees_the_line(w, walked):
    """The letter lands after the line is reserved. Whether the walker takes its edge
    first or the grant's own visit does, no lead row is ever written."""
    lead_id = await parked_then_granted(w)
    assert await enrollment_accessor.resume_run_by_id(
        w.merchant, w.run, "call-1", {"reply_call-1": "LINE_KYC_COMPLETED"}
    )
    if walked:
        await walk(w)

    await grants.GrantWorker()._round()

    assert await leads(w) == []
    assert await node(w) == "quiet"
    assert await w.rr.scard("bb:busy:N1") == 0
    assert await w.rr.hlen("bb:inflight:N1") == 0
    assert await w.rr.zscore(f"bb:q:{w.template}", lead_id) is None
    assert await w.rr.hget(f"bb:qi:{w.template}", lead_id) is None
    assert await w.rr.llen("bb:tickets") == 0


async def test_the_same_grant_delivered_twice_makes_one_lead_and_one_ticket(w):
    lead_id = await parked_then_granted(w)
    entry = await w.rr.lindex("bb:grants", 0)
    worker = grants.GrantWorker()

    await worker._grant(entry)
    await worker._grant(entry)

    assert [lead["id"] for lead in await leads(w)] == [lead_id]
    assert await w.rr.llen("bb:tickets") == 1
    assert await w.rr.zcard(f"bb:q:{w.template}") == 0
    assert await w.rr.smembers("bb:busy:N1") == {f"lead:{lead_id}"}


async def test_a_parked_call_redis_lost_is_put_back_from_the_real_crm_page(w):
    from app.ai.voice.agents.breeze_buddy.dispatch.v2 import reconcile

    await walk(w)
    ((lead_id, score),) = await w.rr.zrange(
        f"bb:q:{w.template}", 0, -1, withscores=True
    )
    await w.rr.delete(f"bb:q:{w.template}", f"bb:qi:{w.template}")
    reconcile._waiting_after = None
    for _ in range(50):  # other tests' parked runs may fill the first pages
        await reconcile.requeue_waiting_calls()
        if reconcile._waiting_after is None:
            break
    assert await w.rr.hget(f"bb:qi:{w.template}", lead_id) == w.run
    back = await w.rr.zscore(f"bb:q:{w.template}", lead_id)
    assert math.floor(back / BAND) == math.floor(score / BAND)  # the same rank
    assert await leads(w) == []


async def test_a_lead_row_names_its_run_and_the_run_gives_its_rank(w):
    from app.crm.outreach.contracts import ranks_for_leads
    from app.database.accessor.breeze_buddy.dispatch import get_lead_dispatch_states

    lead_id = await parked_then_granted(w)
    await grants.GrantWorker()._round()
    state = (await get_lead_dispatch_states([lead_id]))[lead_id]
    assert state.enrollment_id == w.run
    ranks = await ranks_for_leads([(lead_id, state.enrollment_id or "")])
    assert ranks[lead_id]["rank"] == 3

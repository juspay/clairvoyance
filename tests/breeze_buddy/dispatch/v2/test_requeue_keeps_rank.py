"""A lead's rank survives every way back into its room (real Redis; DB accessors mocked).

One rank-3, newest-event lead on a ranked number that is full, so a re-queued lead stays
in its room where its score can be read. Each test drives one re-queue path and expects
the lead in its own band with its own event time. A lost rank would turn a pile lead into
the number's default rank without any error.

Not here, because the code is not on this branch yet: the dial path's re-queues (not
placed, throttled, each deferral, a stopping pod) and a retry's copied rank.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
import app.database.accessor as accessor
from app.ai.voice.agents.breeze_buddy.dispatch import queue as queue_mod
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    reconcile as RC,
    routes,
    scripts,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2.scripts import Rank
from app.database.accessor.breeze_buddy import dispatch as db
from app.database.accessor.breeze_buddy.dispatch import LeadDispatchState as S
from tests.breeze_buddy.dispatch.v2.conftest import seed_number, use_redis

pytestmark = pytest.mark.asyncio

EVENT = 1_791_522_600_123
PRIORITY = {"rank": 3, "order": "newest_event", "event_ms": EVENT}
RANK3 = (3 - 100) * 10**13 + (10**13 - 1) - EVENT  # its ready score
WHEN = datetime.now(timezone.utc) - timedelta(seconds=5)


def NOW() -> int:
    return int(time.time() * 1000)


@pytest.fixture
async def rv(rr, monkeypatch):
    use_redis(monkeypatch, rr, RC, routes, queue_mod)
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(RC, "_is_v2_template", AsyncMock(return_value=True))
    monkeypatch.setattr(RC, "get_live_calls_on_numbers", AsyncMock(return_value={}))
    RC._backlog_after = None
    await seed_number(rr, "N1", 0, {"T1": {}})
    await rr.hset("bb:num:N1", mapping={"ranked": "1", "live_day": "0"})
    await rr.sadd("bb:v2:active", "N1")
    yield rr


def _row(monkeypatch, status: str = "BACKLOG") -> None:
    states = {"L1": S(status, False, "T1", WHEN, PRIORITY)}
    monkeypatch.setattr(RC, "get_lead_dispatch_states", AsyncMock(return_value=states))


async def test_the_db_reads_carry_the_rows_priority(monkeypatch):
    row: dict = {"id": "L1", "template_id": "T1", "next_attempt_at": WHEN}
    row.update(status="BACKLOG", is_locked=False)
    rows = [
        {**row, "priority": json.dumps(PRIORITY)},
        {**row, "id": "L2", "priority": None},
    ]
    monkeypatch.setattr(db, "run_parameterized_query", AsyncMock(return_value=rows))
    assert [r[3] for r in await db.get_due_backlog_page(None, 10)] == [PRIORITY, None]
    states = await db.get_lead_dispatch_states(["L1", "L2"])
    assert (states["L1"].priority, states["L2"].priority) == (PRIORITY, None)


async def test_backlog_job(rv, monkeypatch):
    page = [("L1", "T1", WHEN, PRIORITY)]
    monkeypatch.setattr(RC, "get_due_backlog_page", AsyncMock(side_effect=[page, []]))
    assert await RC.reconcile_backlog_v2() == 1
    assert await rv.zscore("bb:q:T1", "L1") == RANK3


async def test_ledger_requeue(rv, monkeypatch):
    await rv.sadd("bb:busy:N1", "lead:L1")  # holds a line with no lease, no dispatch
    _row(monkeypatch)
    assert await RC.ledger_check() == {"removed": 1}
    assert await rv.zscore("bb:q:T1", "L1") == RANK3


async def test_reaper_requeue(rv, monkeypatch):
    old = NOW() - RC.LEASE_MAX_AGE_MS - 1_000
    lease = {"t": "T1", "tk": 1, "issued_ms": old, "owner": "dead", "claimed_ms": old}
    await rv.sadd("bb:busy:N1", "lead:L1")
    await rv.hset("bb:inflight:N1", "L1", json.dumps(lease))
    _row(monkeypatch)
    assert await RC.reap_leases() == 1
    assert await rv.zscore("bb:q:T1", "L1") == RANK3


async def test_requeue_with_no_rank_given_reads_the_row(rv, monkeypatch):
    lead = NS(metaData={"priority": PRIORITY})
    monkeypatch.setattr(accessor, "get_lead_by_id", AsyncMock(return_value=lead))
    assert await queue_mod.schedule_lead("L1", WHEN, template_id="T1") is True
    assert await rv.zscore("bb:q:T1", "L1") == RANK3


async def _wait_for_later(rv) -> None:
    """L1 waiting for a later time: its due time is its score, its rank is remembered."""
    later = NOW() + 60_000
    assert await scripts.enqueue("T1", "L1", later, rank=Rank(3, "n", EVENT)) == 0
    assert await rv.hget("bb:qp:T1", "L1") == str(RANK3)


async def test_cancel_leaves_no_remembered_score(rv):
    await _wait_for_later(rv)
    assert await queue_mod.cancel_scheduled_lead("L1", template_id="T1") is True
    assert not await rv.exists("bb:q:T1", "bb:qp:T1")


async def test_prune_drops_the_remembered_score_with_the_lead(rv, monkeypatch):
    await _wait_for_later(rv)
    _row(monkeypatch, "FINISHED")
    assert await RC.prune_orphans() == 1
    assert not await rv.exists("bb:q:T1", "bb:qp:T1")


async def test_hand_back_to_todays_schedule_leaves_no_negative_score(rv):
    assert await scripts.enqueue("T1", "L1", NOW(), rank=Rank(3, "n", EVENT)) == 0
    assert await rv.zscore("bb:q:T1", "L1") == RANK3
    assert await scripts.move_room_to_schedule("T1") == 1
    assert 0 <= NOW() - await rv.zscore("bb:schedule:leads", "L1") < 5_000

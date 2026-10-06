"""waiting_calls_page: the calls waiting for a line with no lead row yet.

The dialler rebuilds its queue from here (it lost Redis, or a paused number
resumes): the runs holding on a call square that lists topics, a page at a
time, each with the lead id that square queued and the rank it has now.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, List
from uuid import UUID

import pytest

import app.crm.outreach.nodes.call as call_node
from app.crm.outreach.contracts import waiting_calls_page
from app.crm.outreach.db.queries.grant import (
    parking_squares_query,
    waiting_runs_page_query,
)
from app.crm.outreach.schemas import EnrollmentRun
from tests.crm.conftest import CRM_WEBHOOK_TEST_DSN as DSN
from tests.crm.test_call_parking import PLAN
from tests.crm.test_call_priority import _context, _ist, _ms
from tests.crm.test_grant import _install, _lead_id, _World

WAKE = datetime(2026, 11, 1, tzinfo=timezone.utc)


class _Table(_World):
    """test_grant's world, plus the two reads a page makes. The squares are a
    net wider than the answer: `quiet` listens too, but it is not a call."""

    squares: Any = (["m1"], [], ["call-1", "quiet"])
    reads = 0

    async def parking_squares(self) -> Any:
        return self.squares

    async def waiting_runs_page(
        self, merchants: Any, workflows: Any, nodes: Any, after: Any, limit: int
    ) -> List[EnrollmentRun]:
        self.reads += 1
        rows = sorted(
            (
                (r.wake_at, str(r.id), r)
                for r in self.runs.values()
                if r.status == "waiting" and r.current_node in nodes
            ),
            key=lambda row: row[:2],
        )
        return [r for *key, r in rows if not after or tuple(key) > after][:limit]


async def test_a_page_lists_the_calls_waiting_for_a_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _install(monkeypatch, _Table(PLAN))
    monkeypatch.setattr(call_node, "_now", lambda: _ist(9, 10, 30))
    live_at, night = _ist(9, 10, 5), _ist(9, 2, 0)
    live = w.waiting(wake_at=WAKE, context=_context("LINE_OFFERED", live_at))
    pile = w.waiting(
        wake_at=WAKE + timedelta(hours=1),
        context=_context("LINE_KYC_COMPLETED", night),
    )
    last = w.waiting(wake_at=WAKE + timedelta(hours=2))
    w.waiting(wake_at=WAKE, current_node="quiet")  # in the net, not on a call

    first, after = await waiting_calls_page(None, 3)
    rest, end = await waiting_calls_page(after, 3)

    assert first == [
        {
            "run_id": str(live.id),
            "lead_id": _lead_id(live),
            "template_id": "tpl-1",
            "priority": {
                "rank": 1,
                "order": "first_ready",
                "event_ms": _ms(live_at),
                "next_rank": 3,
                "next_order": "newest_event",
            },
        },
        {
            "run_id": str(pile.id),
            "lead_id": _lead_id(pile),
            "template_id": "tpl-1",
            "priority": {"rank": 2, "order": "newest_event", "event_ms": _ms(night)},
        },
    ]
    assert after == (pile.wake_at, str(pile.id))
    assert [call["run_id"] for call in rest] == [str(last.id)]
    assert end is None  # a short read: there is no next page


async def test_with_no_square_that_waits_for_a_line_no_run_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _install(monkeypatch, _Table(PLAN))
    w.squares = (None, None, None)  # pyrefly: ignore[missing-attribute]
    w.waiting()

    assert await waiting_calls_page(None, 10) == ([], None)
    assert w.reads == 0  # pyrefly: ignore[missing-attribute]


@pytest.mark.skipif(not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run against Postgres")
async def test_on_postgres_the_two_reads_find_the_waiting_runs_in_order() -> None:
    """Against TEMP tables shaped like the columns the statements touch. A
    square counts when a version of a plan a token may move on has a call that
    lists topics; the runs on it come back in (wake_at, id) order, after the
    last one read."""
    import asyncpg

    live, paused = UUID(int=1), UUID(int=2)
    document = {
        "nodes": [
            {"id": "call-1", "type": "call", "topics": ["X"]},
            {"id": "quiet", "type": "wait", "topics": ["X"]},
            {"id": "call-2", "type": "call", "topics": []},
            {"id": "call-3", "type": "call", "topics": None},
            {"id": "call-4", "type": "call"},
        ]
    }
    runs = [
        (UUID(int=11), live, "waiting", "call-1", WAKE + timedelta(hours=1)),
        (UUID(int=12), live, "waiting", "call-1", WAKE),
        (UUID(int=13), live, "waiting", "call-1", WAKE),
        (UUID(int=14), live, "waiting", "quiet", WAKE),
        (UUID(int=15), live, "parked", "call-1", None),
        (UUID(int=16), paused, "waiting", "call-1", WAKE),
    ]
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "CREATE TEMP TABLE crm_workflow (merchant_id text, id uuid, status text);"
            "CREATE TEMP TABLE crm_workflow_version (merchant_id text,"
            " workflow_id uuid, version int, definition jsonb);"
            "CREATE TEMP TABLE crm_workflow_enrollment (id uuid, workflow_id uuid,"
            " status text, current_node text, wake_at timestamptz,"
            " merchant_id text DEFAULT 'm1', workflow_version int, customer_id uuid,"
            " entered_at timestamptz, exited_at timestamptz, exit_reason text,"
            " context jsonb, enrollment_key text, attempts int, last_error text,"
            " node_arrived_at timestamptz)"
        )
        await conn.execute(
            "INSERT INTO crm_workflow VALUES ('m1', $1, 'live'), ('m1', $2, 'paused')",
            live,
            paused,
        )
        await conn.execute(
            "INSERT INTO crm_workflow_version VALUES"
            " ('m1', $1, 1, $3::jsonb), ('m1', $2, 1, $3::jsonb)",
            live,
            paused,
            json.dumps(document),
        )
        await conn.executemany(
            "INSERT INTO crm_workflow_enrollment VALUES ($1, $2, $3, $4, $5)", runs
        )
        sql, params = parking_squares_query()
        squares = tuple(await conn.fetchrow(sql, *params))
        pages = []
        for after in (None, (WAKE, str(UUID(int=13)))):
            # pyrefly: ignore[bad-argument-count]
            sql, params = waiting_runs_page_query(*squares, after, 2)
            pages.append([row["id"].int for row in await conn.fetch(sql, *params)])
        # The paused plan goes live again: its waiting run is listed, so a call
        # the dialler dropped on a PAUSED refusal is queued by the next rebuild.
        await conn.execute("UPDATE crm_workflow SET status = 'live'")
        sql, params = parking_squares_query()
        net = await conn.fetchrow(sql, *params)
        assert net is not None
        sql, params = waiting_runs_page_query(net[0], net[1], net[2], None, 9)
        resumed = [row["id"].int for row in await conn.fetch(sql, *params)]
    finally:
        await conn.close()

    assert squares == (["m1"], [live], ["call-1"])
    assert pages == [[12, 13], [11]]
    assert resumed == [12, 13, 16, 11]

"""Broadcasts phase 2 (canon T17 + T18): the launch — schedule, cancel — and
the freeze the broadcaster loop runs at the broadcast's moment.

What is proven here:
- 090 keeps the canon's self-defense: the recipient's PK is the dedupe, its
  status words are the schema's, a reason exactly when skipped, no
  merchant_id (a child row), the customer IX and the touch trigger; the
  freeze's claim index is partial on the broadcasts in flight;
- the builders: the claim takes due broadcasts AND freezes whose lease ran
  out, stamps resolved_at, SKIP LOCKED; schedule only from draft/scheduled;
  finish and fail only from resolving; the recipient insert is one unnest
  with ON CONFLICT DO NOTHING that answers with one count; cancel touches
  queued rows only; every value a parameter, every parameter referenced;
- schedule: the workflow must be live, the moment needs a timezone and may
  not be past (every problem at once), a draft is aimed and a scheduled one
  re-aimed, anything later is a conflict — including one that moved between
  the read and the update;
- cancel: draft / scheduled / resolving / dispatching -> cancelled with the
  still-queued rows cancelled in the same atom, enrolled rows untouched;
  finished broadcasts are a conflict;
- the freeze: every page queued, then dispatching; an empty segment
  dispatches nobody; a re-run (a dead worker's freeze taken over) adds no one
  twice; a cancel between pages stops it before the next page lands; a
  segment that cannot be answered fails the broadcast;
- the freeze reads ids, never member objects: a static list's page is its
  ids through the parent and by key, a dynamic segment's is record's answer,
  and a rule record cannot answer is invalid;
- the walker role runs the broadcaster beside the walker, one broadcast at a
  time;
- the routes answer 200 / 404 / 409 / 422.
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple, get_args
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.crm.outreach.broadcasts as broadcasts
import app.crm.outreach.db.accessors.broadcast_recipient as recipient_accessor
import app.crm.outreach.segments as segments
import app.crm.worker_main as worker_main
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.outreach import api as outreach_api
from app.crm.outreach.db.queries.broadcast import (
    claim_due_freezes_query,
    fail_freeze_query,
    finish_freeze_query,
    lock_broadcast_query,
    schedule_broadcast_query,
)
from app.crm.outreach.db.queries.broadcast_recipient import (
    cancel_queued_recipients_query,
    insert_recipients_query,
)
from app.crm.outreach.db.queries.segment_member import member_ids_query
from app.crm.outreach.schemas import Broadcast, RecipientStatus
from app.crm.outreach.segments import SegmentNotFound, SegmentRuleInvalid
from app.crm.record.contracts import SegmentRuleUnanswerable
from scripts.check_crm_boundaries import TABLE_OWNERS

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
MIGRATION = Path("app/database/migrations/090_create_crm_broadcast_recipient.sql")
HOSTILE = "x'); DROP TABLE crm_broadcast_recipient; --"


def _people(n: int) -> List[str]:
    return [str(UUID(int=i + 1)) for i in range(n)]


def _broadcast(status: str = "draft", **overrides: Any) -> Broadcast:
    row: Dict[str, Any] = {
        "id": uuid4(),
        "merchant_id": "m1",
        "name": "Final nudge",
        "segment_id": uuid4(),
        "workflow_id": uuid4(),
        "status": status,
        "created_at": NOW,
    }
    row.update(overrides)
    return Broadcast(**row)


class World:
    """The broadcast and recipient accessors, the workflow status read and
    the segment's pages, as one fake store."""

    def __init__(self, broadcast: Optional[Broadcast], workflow_status: str = "live"):
        self.broadcast = broadcast
        self.workflow_status = workflow_status
        self.recipients: Dict[str, str] = {}  # customer -> status
        self.audience: List[str] = []
        self.unanswerable: Optional[Exception] = None
        self.between_pages: Optional[Callable[[int], None]] = None
        self.pages_read = 0

    # --- the broadcast accessor ---
    async def get_broadcast(self, merchant_id: str, broadcast_id: str):
        b = self.broadcast
        return (
            b
            if b and b.merchant_id == merchant_id and str(b.id) == broadcast_id
            else None
        )

    async def schedule_broadcast(self, merchant_id: str, broadcast_id: str, at):
        b = await self.get_broadcast(merchant_id, broadcast_id)
        if b is None or b.status not in ("draft", "scheduled"):
            return None
        self._set(status="scheduled", scheduled_for=at or NOW)
        return self.broadcast

    async def lock_broadcast(self, conn: Any, merchant_id: str, broadcast_id: str):
        b = await self.get_broadcast(merchant_id, broadcast_id)
        return b.status if b else None

    async def cancel_broadcast(self, conn: Any, merchant_id: str, broadcast_id: str):
        self._set(status="cancelled")
        return self.broadcast

    async def finish_freeze(self, merchant_id: str, broadcast_id: str):
        if self.broadcast and self.broadcast.status == "resolving":
            self._set(status="dispatching", dispatched_at=NOW)
            return self.broadcast
        return None

    async def fail_freeze(self, merchant_id: str, broadcast_id: str):
        if self.broadcast and self.broadcast.status == "resolving":
            self._set(status="failed")
            return self.broadcast
        return None

    def _set(self, **changes: Any) -> None:
        assert self.broadcast is not None
        self.broadcast = self.broadcast.model_copy(update=changes)

    # --- the recipient accessor ---
    async def insert_recipients(
        self, conn: Any, broadcast_id: str, ids: List[str]
    ) -> int:
        new = [c for c in ids if c not in self.recipients]
        self.recipients.update({c: "queued" for c in new})
        return len(new)

    async def cancel_queued_recipients(self, conn: Any, broadcast_id: str) -> int:
        queued = [c for c, s in self.recipients.items() if s == "queued"]
        self.recipients.update({c: "cancelled" for c in queued})
        return len(queued)

    # --- the workflow status read ---
    async def read_workflow_status(self, merchant_id: str, workflow_id: str):
        return self.workflow_status

    # --- outreach.segments.segment_member_ids ---
    async def segment_member_ids(
        self, merchant_id: str, segment_id: str, after: Optional[str], limit: int
    ) -> List[str]:
        if self.unanswerable:
            raise self.unanswerable
        if self.pages_read and self.between_pages:
            self.between_pages(self.pages_read)
        self.pages_read += 1
        start = self.audience.index(after) + 1 if after else 0
        return self.audience[start : start + limit]


async def _atomically(fn: Any, *args: Any) -> Any:
    return await fn(None, *args)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    def _install(broadcast: Optional[Broadcast], **kw: Any) -> World:
        w = World(broadcast, **kw)
        monkeypatch.setattr(broadcasts, "broadcast_accessor", w)
        monkeypatch.setattr(broadcasts, "recipient_accessor", w)
        monkeypatch.setattr(
            broadcasts,
            "workflow_accessor",
            SimpleNamespace(workflow_status=w.read_workflow_status),
        )
        monkeypatch.setattr(broadcasts, "segment_member_ids", w.segment_member_ids)
        monkeypatch.setattr(broadcasts, "atomically", _atomically)
        monkeypatch.setattr(broadcasts, "FREEZE_PAGE", 1000)
        return w

    return _install


# --- the table ---


def test_the_recipient_table_has_an_owner() -> None:
    assert TABLE_OWNERS["crm_broadcast_recipient"] == "outreach"


def test_090_keeps_the_canon_self_defense() -> None:
    sql = MIGRATION.read_text()
    table = sql.split("CREATE TABLE crm_broadcast_recipient (")[1].split("\n);")[0]
    assert "PRIMARY KEY (broadcast_id, customer_id)" in table  # the dedupe
    assert "merchant_id" not in table  # a child row rides the broadcast
    assert "broadcast_id  uuid NOT NULL REFERENCES crm_broadcast (id)" in table
    enum = re.search(r"CHECK \(status IN \(([^)]*)\)\)", table)
    assert enum is not None
    assert re.findall(r"'([a-z]+)'", enum.group(1)) == list(get_args(RecipientStatus))
    assert "CHECK ((status = 'skipped') = (skip_reason IS NOT NULL))" in table
    assert "ON crm_broadcast_recipient (customer_id)" in sql
    assert "EXECUTE FUNCTION crm_touch_updated_at()" in sql
    assert "ON crm_broadcast (scheduled_for)" in sql
    assert "WHERE status IN ('scheduled', 'resolving')" in sql


# --- the builders ---


@pytest.mark.parametrize(
    "built",
    [
        schedule_broadcast_query("m1", HOSTILE, NOW),
        schedule_broadcast_query("m1", HOSTILE, None),
        lock_broadcast_query("m1", HOSTILE),
        finish_freeze_query("m1", HOSTILE),
        fail_freeze_query("m1", HOSTILE),
        insert_recipients_query(HOSTILE, [HOSTILE]),
        cancel_queued_recipients_query(HOSTILE),
        claim_due_freezes_query(1, 15),
    ],
)
def test_every_builder_is_parameterised(built: Tuple[str, List[Any]]) -> None:
    sql, params = built
    assert HOSTILE not in sql
    assert {int(n) for n in re.findall(r"\$(\d+)", sql)} == set(
        range(1, len(params) + 1)
    )


def test_the_claim_takes_due_broadcasts_and_dead_freezes() -> None:
    sql, params = claim_due_freezes_query(1, 15)
    assert params == [1, 15]
    assert "SET status = 'resolving', resolved_at = now()" in sql
    assert "(status = 'scheduled' AND scheduled_for <= now())" in sql
    assert "status = 'resolving'" in sql
    assert "resolved_at <= now() - make_interval(mins => $2)" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql and "LIMIT $1" in sql


def test_schedule_moves_only_a_draft_or_a_scheduled_broadcast() -> None:
    sql, params = schedule_broadcast_query("m1", "b1", None)
    assert (
        "WHERE merchant_id = $1 AND id = $2 AND status IN ('draft', 'scheduled')" in sql
    )
    assert "scheduled_for = COALESCE($3::timestamptz, now())" in sql
    assert params == ["m1", "b1", None]


def test_finish_and_fail_move_only_a_freeze_in_progress() -> None:
    finish, _ = finish_freeze_query("m1", "b1")
    assert "SET status = 'dispatching', dispatched_at = now()" in finish
    fail, _ = fail_freeze_query("m1", "b1")
    assert "SET status = 'failed'" in fail
    for sql in (finish, fail):
        assert "WHERE merchant_id = $1 AND id = $2 AND status = 'resolving'" in sql


def test_the_lock_is_merchant_first_for_update() -> None:
    sql, params = lock_broadcast_query("m1", "b1")
    assert "WHERE merchant_id = $1 AND id = $2" in sql and "FOR UPDATE" in sql
    assert params == ["m1", "b1"]


def test_a_freeze_page_is_one_statement_and_a_repeat_is_a_no_op() -> None:
    sql, params = insert_recipients_query("b1", ["c1", "c2"])
    assert "FROM unnest($2::uuid[]) AS customer_id" in sql
    assert "ON CONFLICT (broadcast_id, customer_id) DO NOTHING" in sql
    # One number back, not the page's ids: the freeze needs only the count.
    assert "RETURNING 1" in sql and "SELECT count(*) AS added FROM added" in sql
    assert "RETURNING customer_id" not in sql
    assert params == ["b1", ["c1", "c2"]]
    cancel, _ = cancel_queued_recipients_query("b1")
    assert "WHERE broadcast_id = $1 AND status = 'queued'" in cancel


# --- schedule ---


@pytest.mark.parametrize(
    ("workflow_status", "at", "problems"),
    [
        ("live", None, []),
        ("live", NOW + timedelta(days=3), []),
        ("draft", None, ["workflow_id: the workflow is draft"]),
        ("paused", None, ["workflow_id: the workflow is paused"]),
        (None, None, ["workflow_id: the workflow is gone"]),
        ("live", datetime(2026, 10, 17, 10, 0), ["scheduled_for: needs a timezone"]),
        ("live", NOW - timedelta(minutes=5), ["scheduled_for: is in the past"]),
        (
            "paused",
            NOW - timedelta(minutes=5),
            ["workflow_id: the workflow is paused", "scheduled_for: is in the past"],
        ),
    ],
)
def test_schedule_problems(
    workflow_status: Optional[str], at: Optional[datetime], problems: List[str]
) -> None:
    found = broadcasts.schedule_problems(workflow_status, at, NOW)
    assert len(found) == len(problems)
    assert all(p.startswith(expected) for p, expected in zip(found, problems))


async def test_a_draft_is_aimed_and_a_scheduled_one_re_aimed(world) -> None:
    w = world(_broadcast("draft"))
    later = datetime.now(timezone.utc) + timedelta(days=2)
    aimed = await broadcasts.schedule_broadcast("m1", str(w.broadcast.id), later)
    assert aimed.status == "scheduled" and aimed.scheduled_for == later
    sooner = datetime.now(timezone.utc) + timedelta(hours=1)
    again = await broadcasts.schedule_broadcast("m1", str(w.broadcast.id), sooner)
    assert again.scheduled_for == sooner


@pytest.mark.parametrize("status", ["resolving", "dispatching", "sent", "cancelled"])
async def test_a_launched_or_finished_broadcast_cannot_be_aimed(
    world, status: str
) -> None:
    w = world(_broadcast(status))
    with pytest.raises(broadcasts.BroadcastStateConflict) as refused:
        await broadcasts.schedule_broadcast("m1", str(w.broadcast.id), None)
    assert refused.value.status == status


async def test_a_workflow_that_is_not_live_aims_nothing(world) -> None:
    w = world(_broadcast("draft"), workflow_status="paused")
    with pytest.raises(broadcasts.BroadcastInvalid, match="paused"):
        await broadcasts.schedule_broadcast("m1", str(w.broadcast.id), None)
    assert w.broadcast.status == "draft"


async def test_a_broadcast_that_moved_before_the_update_says_where_it_is(world) -> None:
    w = world(_broadcast("draft"))

    async def moved(merchant_id: str, broadcast_id: str, at: Any) -> None:
        w._set(status="cancelled")  # cancelled between the read and the update
        return None

    w.schedule_broadcast = moved  # type: ignore[method-assign]
    with pytest.raises(broadcasts.BroadcastStateConflict) as refused:
        await broadcasts.schedule_broadcast("m1", str(w.broadcast.id), None)
    assert refused.value.status == "cancelled"


async def test_another_merchants_broadcast_is_not_found(world) -> None:
    w = world(_broadcast("draft", merchant_id="m2"))
    with pytest.raises(broadcasts.BroadcastNotFound):
        await broadcasts.schedule_broadcast("m1", str(w.broadcast.id), None)
    with pytest.raises(broadcasts.BroadcastNotFound):
        await broadcasts.cancel_broadcast("m1", str(w.broadcast.id))


# --- cancel ---


@pytest.mark.parametrize("status", ["draft", "scheduled", "resolving", "dispatching"])
async def test_cancel_stops_admissions_and_cancels_only_the_queue(
    world, status: str
) -> None:
    w = world(_broadcast(status))
    w.recipients = {"c1": "queued", "c2": "enrolled", "c3": "skipped"}
    cancelled = await broadcasts.cancel_broadcast("m1", str(w.broadcast.id))
    assert cancelled.status == "cancelled"
    assert w.recipients == {"c1": "cancelled", "c2": "enrolled", "c3": "skipped"}


@pytest.mark.parametrize("status", ["sent", "closed", "cancelled", "failed"])
async def test_a_finished_broadcast_cannot_be_cancelled(world, status: str) -> None:
    w = world(_broadcast(status))
    w.recipients = {"c1": "queued"}
    with pytest.raises(broadcasts.BroadcastStateConflict):
        await broadcasts.cancel_broadcast("m1", str(w.broadcast.id))
    assert w.recipients == {"c1": "queued"}


# --- the freeze ---


async def test_the_freeze_queues_every_page_then_dispatches(world) -> None:
    w = world(_broadcast("resolving", resolved_at=NOW))
    w.audience = _people(2500)
    await broadcasts.freeze_broadcast(w.broadcast)
    assert len(w.recipients) == 2500 and set(w.recipients.values()) == {"queued"}
    assert w.pages_read == 3
    assert w.broadcast.status == "dispatching"


async def test_an_empty_segment_dispatches_nobody(world) -> None:
    w = world(_broadcast("resolving"))
    await broadcasts.freeze_broadcast(w.broadcast)
    assert w.recipients == {} and w.broadcast.status == "dispatching"


async def test_a_taken_over_freeze_adds_no_one_twice(world) -> None:
    w = world(_broadcast("resolving"))
    w.audience = _people(1500)
    w.recipients = {c: "queued" for c in w.audience[:1000]}  # the dead worker's page
    await broadcasts.freeze_broadcast(w.broadcast)
    assert len(w.recipients) == 1500


async def test_a_cancel_between_pages_stops_the_freeze(world) -> None:
    w = world(_broadcast("resolving"))
    w.audience = _people(2500)
    w.between_pages = lambda pages: w._set(status="cancelled")
    await broadcasts.freeze_broadcast(w.broadcast)
    assert len(w.recipients) == 1000  # page 1 only
    assert w.broadcast.status == "cancelled"  # never dispatching


@pytest.mark.parametrize(
    "error", [SegmentRuleInvalid(["rule.where[0]: no"]), SegmentNotFound("s1")]
)
async def test_a_segment_that_cannot_be_answered_fails_the_broadcast(
    world, error: Exception
) -> None:
    w = world(_broadcast("resolving"))
    w.unanswerable = error
    await broadcasts.freeze_broadcast(w.broadcast)
    assert w.broadcast.status == "failed" and w.recipients == {}


# --- the freeze's read: ids, never member objects ---


def test_a_freeze_page_is_ids_only_through_the_parent_and_by_key() -> None:
    sql, params = member_ids_query("m1", "seg-1", "c-9", 1000)
    assert re.search(r"SELECT m\.customer_id\s+FROM crm_segment_member m", sql)
    assert "added_at" not in sql and "source" not in sql
    assert "JOIN crm_segment s ON s.id = m.segment_id" in sql
    assert "WHERE s.merchant_id = $1" in sql and "AND m.segment_id = $2" in sql
    assert "m.customer_id > $3::uuid" in sql and "ORDER BY m.customer_id" in sql
    assert params == ["m1", "seg-1", "c-9", 1000]


async def test_the_insert_answers_with_the_count_it_added() -> None:
    class Conn:
        seen: Tuple[str, Tuple[Any, ...]] = ("", ())

        async def fetchval(self, query: str, *values: Any) -> int:
            self.seen = (query, values)
            return 7

    conn: Any = Conn()
    assert await recipient_accessor.insert_recipients(conn, "b1", ["c1"]) == 7
    assert "SELECT count(*) AS added FROM added" in conn.seen[0]


@pytest.mark.parametrize("kind", ["static", "dynamic"])
async def test_a_freeze_reads_a_lists_ids_or_records_answer(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    asked: List[Tuple[Any, ...]] = []
    rule = None if kind == "static" else {"event": "OFFERED"}

    async def _segment(merchant_id: str, segment_id: str) -> Any:
        return SimpleNamespace(kind=kind, definition=rule)

    async def member_ids(*args: Any) -> List[str]:
        asked.append(("list", *args))
        return ["c1", "c2"]

    async def customers_matching(*args: Any) -> List[str]:
        asked.append(("record", *args))
        return ["c3"]

    monkeypatch.setattr(segments, "_segment", _segment)
    monkeypatch.setattr(segments.segment_member_accessor, "member_ids", member_ids)
    monkeypatch.setattr(segments, "customers_matching", customers_matching)
    ids = await segments.segment_member_ids("m1", "seg-1", "c0", 2)
    if kind == "static":
        assert ids == ["c1", "c2"] and asked == [("list", "m1", "seg-1", "c0", 2)]
    else:
        assert ids == ["c3"] and asked == [("record", "m1", rule, "c0", 2)]


async def test_a_rule_record_cannot_answer_is_invalid_for_the_freeze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _segment(*args: Any) -> Any:
        return SimpleNamespace(kind="dynamic", definition={"event": "OFFERED"})

    async def customers_matching(*args: Any) -> List[str]:
        raise SegmentRuleUnanswerable(["rule.where[0]: no SQL"])

    monkeypatch.setattr(segments, "_segment", _segment)
    monkeypatch.setattr(segments, "customers_matching", customers_matching)
    with pytest.raises(SegmentRuleInvalid) as raised:
        await segments.segment_member_ids("m1", "seg-1", None, 10)
    assert raised.value.problems == ["rule.where[0]: no SQL"]


# --- the worker ---


async def test_the_walker_role_runs_the_broadcaster_beside_the_walker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loops: List[Dict[str, Any]] = []

    async def drain(claim: Any, handle: Any, **kw: Any) -> None:
        loops.append({"claim": claim, "handle": handle, **kw})

    monkeypatch.setattr(worker_main, "run_drain_loop", drain)
    await worker_main.ROLES["walker"](asyncio.Event())
    assert [loop["name"] for loop in loops] == ["walker", "broadcaster"]
    broadcaster = loops[1]
    assert broadcaster["claim"] is worker_main.claim_due_freezes
    assert broadcaster["handle"] is worker_main.freeze_broadcast
    assert broadcaster["batch"] == 1


# --- the routes ---


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(outreach_api.broadcast_router, prefix="/broadcasts")
    app.dependency_overrides[get_current_user_with_rbac] = lambda: SimpleNamespace(
        role="user", username="u", merchant_ids=["m1"], reseller_ids=[], email="ops@x"
    )
    return TestClient(app)


def test_the_schedule_route(world) -> None:
    w = world(_broadcast("draft"))
    path = f"/broadcasts/{w.broadcast.id}/schedule?merchant_id=m1"
    later = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    aimed = _client().post(path, json={"scheduled_for": later})
    assert aimed.status_code == 200 and aimed.json()["status"] == "scheduled"
    naive = _client().post(path, json={"scheduled_for": "2026-12-01T10:00:00"})
    assert naive.status_code == 422
    assert naive.json()["detail"][0].startswith("scheduled_for: needs a timezone")
    w.broadcast = w.broadcast.model_copy(update={"status": "dispatching"})
    assert _client().post(path, json={}).status_code == 409
    missing = f"/broadcasts/{uuid4()}/schedule?merchant_id=m1"
    assert _client().post(missing, json={}).status_code == 404


def test_the_cancel_route(world) -> None:
    w = world(_broadcast("scheduled"))
    path = f"/broadcasts/{w.broadcast.id}/cancel?merchant_id=m1"
    cancelled = _client().post(path)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert _client().post(path).status_code == 409  # already cancelled
    missing = f"/broadcasts/{uuid4()}/cancel?merchant_id=m1"
    assert _client().post(missing).status_code == 404

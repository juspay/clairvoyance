"""Broadcasts phase 2 (canon T17 + T18): the launch — schedule, cancel — and
the freeze the broadcaster loop runs at the broadcast's moment.

What is proven here:
- 091 keeps the canon's self-defense: the recipient's PK is the dedupe, its
  status words are the schema's, a reason exactly when skipped, no
  merchant_id (a child row), the customer IX and the touch trigger; the
  freeze's claim index is partial on the broadcasts in flight;
- 092 adds attempts (never below zero) and last_error, and says why it
  deviates from T17;
- the builders: the claim takes due broadcasts AND freezes whose lease ran
  out, stamps resolved_at, spends an attempt, SKIP LOCKED; schedule only
  from draft/scheduled; finish, fail, the lease renewal and the error record
  only from resolving; finish leaves dispatched_at to phase 3; fail keeps
  its reason; the resume read enters through the parent; the recipient
  insert is one unnest with ON CONFLICT DO NOTHING that answers with one
  count; cancel touches queued rows only; every value a parameter, every
  parameter referenced;
- schedule: the workflow must be live, the moment needs a timezone and may
  not be past beyond a minute's grace (every problem at once), a draft is
  aimed and a scheduled one re-aimed, anything later is a conflict —
  including one that moved between the read and the update;
- cancel: draft / scheduled / resolving / dispatching -> cancelled with the
  still-queued rows cancelled in the same atom, enrolled rows untouched;
  finished broadcasts are a conflict;
- the freeze: every page queued and the lease renewed in the page's own
  atom, then dispatching; an empty segment dispatches nobody; a re-run (a
  dead worker's freeze taken over) resumes after the last queued id and adds
  no one twice; a cancel between pages stops it before the next page lands;
  a segment that cannot be answered fails the broadcast with its reason;
  any other error is kept as last_error and raised; a broadcast past its
  attempts fails with the last error;
- the freeze reads ids, never member objects: a static list's page is its
  ids through the parent and by key, a dynamic segment's is record's answer,
  and a rule record cannot answer is invalid;
- the walker role is the walker's one loop; the broadcaster is its own
  role, one broadcast at a time;
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
    get_broadcast_query,
    lock_broadcast_query,
    record_freeze_error_query,
    renew_freeze_lease_query,
    schedule_broadcast_query,
)
from app.crm.outreach.db.queries.broadcast_recipient import (
    cancel_queued_recipients_query,
    insert_recipients_query,
    last_frozen_customer_query,
)
from app.crm.outreach.db.queries.segment_member import member_ids_query
from app.crm.outreach.schemas import Broadcast, RecipientStatus
from app.crm.outreach.segments import SegmentNotFound, SegmentRuleInvalid
from app.crm.record.contracts import SegmentRuleUnanswerable
from scripts.check_crm_boundaries import TABLE_OWNERS

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
MIGRATION = Path("app/database/migrations/091_create_crm_broadcast_recipient.sql")
ATTEMPTS_MIGRATION = Path(
    "app/database/migrations/092_add_crm_broadcast_freeze_attempts.sql"
)
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
        self.afters: List[Optional[str]] = []  # where each page read began
        self.writes: List[Tuple[Any, str]] = []  # (the atom's txn, what)
        self.atoms = 0
        self.broken: Optional[Exception] = None  # a transient error, page 2

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
            # dispatched_at is phase 3's; a retry that worked leaves nothing.
            self._set(status="dispatching", attempts=0, last_error=None)
            return self.broadcast
        return None

    async def fail_freeze(
        self, conn: Any, merchant_id: str, broadcast_id: str, error: str
    ):
        self.writes.append((conn, "fail"))
        if self.broadcast and self.broadcast.status == "resolving":
            self._set(status="failed", last_error=error)
            return self.broadcast
        return None

    async def record_freeze_error(
        self, merchant_id: str, broadcast_id: str, error: str
    ) -> None:
        if self.broadcast and self.broadcast.status == "resolving":
            self._set(last_error=error)

    async def renew_freeze_lease(
        self, conn: Any, merchant_id: str, broadcast_id: str
    ) -> None:
        self.writes.append((conn, "renew"))

    def _set(self, **changes: Any) -> None:
        assert self.broadcast is not None
        self.broadcast = self.broadcast.model_copy(update=changes)

    # --- the recipient accessor ---
    async def insert_recipients(
        self, conn: Any, broadcast_id: str, ids: List[str]
    ) -> int:
        self.writes.append((conn, "insert"))
        new = [c for c in ids if c not in self.recipients]
        self.recipients.update({c: "queued" for c in new})
        return len(new)

    async def last_frozen_customer(
        self, merchant_id: str, broadcast_id: str
    ) -> Optional[str]:
        return max(self.recipients, key=UUID) if self.recipients else None

    async def cancel_queued_recipients(self, conn: Any, broadcast_id: str) -> int:
        self.writes.append((conn, "cancel_queue"))
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
        if self.pages_read and self.broken:
            raise self.broken
        if self.pages_read and self.between_pages:
            self.between_pages(self.pages_read)
        self.pages_read += 1
        self.afters.append(after)
        start = self.audience.index(after) + 1 if after else 0
        return self.audience[start : start + limit]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    def _install(broadcast: Optional[Broadcast], **kw: Any) -> World:
        w = World(broadcast, **kw)

        async def _atomically(fn: Any, *args: Any) -> Any:
            w.atoms += 1  # a txn per atom, so a test can see what shared one
            return await fn(f"txn-{w.atoms}", *args)

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


def test_091_keeps_the_canon_self_defense() -> None:
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
    # T18 col 8 IX: partial over the rows still waiting, keyed by the parent.
    waiting = sql.split("CREATE INDEX crm_broadcast_recipient_waiting_ix")[1]
    waiting = waiting.split(";")[0]
    assert "ON crm_broadcast_recipient (broadcast_id, customer_id)" in waiting
    assert "WHERE status IN ('queued', 'claimed')" in waiting
    assert "EXECUTE FUNCTION crm_touch_updated_at()" in sql
    assert "ON crm_broadcast (scheduled_for)" in sql
    assert "WHERE status IN ('scheduled', 'resolving')" in sql


def test_092_adds_the_freeze_attempts_and_says_why_it_deviates() -> None:
    sql = ATTEMPTS_MIGRATION.read_text()
    assert "ALTER TABLE crm_broadcast" in sql
    assert "ADD COLUMN attempts smallint NOT NULL DEFAULT 0" in sql
    assert "ADD COLUMN last_error text" in sql
    assert "CHECK (attempts >= 0)" in sql
    header = "".join(line for line in sql.splitlines() if line.startswith("--"))
    assert "T17" in header and "T20" in header and "deviation" in header


def test_a_broadcast_read_carries_its_attempts_and_last_error() -> None:
    sql, _ = get_broadcast_query("m1", "b1")
    assert "attempts, last_error" in sql


# --- the builders ---


@pytest.mark.parametrize(
    "built",
    [
        schedule_broadcast_query("m1", HOSTILE, NOW),
        schedule_broadcast_query("m1", HOSTILE, None),
        lock_broadcast_query("m1", HOSTILE),
        finish_freeze_query("m1", HOSTILE),
        fail_freeze_query("m1", HOSTILE, HOSTILE),
        renew_freeze_lease_query("m1", HOSTILE),
        record_freeze_error_query("m1", HOSTILE, HOSTILE),
        insert_recipients_query(HOSTILE, [HOSTILE]),
        cancel_queued_recipients_query(HOSTILE),
        last_frozen_customer_query("m1", HOSTILE),
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
    assert (
        "SET status = 'resolving', resolved_at = now(), attempts = attempts + 1" in sql
    )
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


def test_finish_fail_renew_and_record_touch_only_a_freeze_in_progress() -> None:
    finish, _ = finish_freeze_query("m1", "b1")
    # A retry that worked leaves nothing: cleared as a run's are (T20).
    assert "SET status = 'dispatching', attempts = 0, last_error = NULL" in finish
    # "fired" is the first admission (canon T17 col 18) — phase 3 stamps it.
    assert "dispatched_at" not in finish.split("RETURNING")[0]
    fail, fail_params = fail_freeze_query("m1", "b1", "why")
    assert "SET status = 'failed', last_error = $3" in fail
    assert fail_params == ["m1", "b1", "why"]
    renew, _ = renew_freeze_lease_query("m1", "b1")
    assert "SET resolved_at = now()" in renew
    record, record_params = record_freeze_error_query("m1", "b1", "why")
    assert re.search(r"SET last_error = \$3\s+WHERE", record)  # status untouched
    assert record_params == ["m1", "b1", "why"]
    for sql in (finish, fail, renew, record):
        assert "WHERE merchant_id = $1 AND id = $2 AND status = 'resolving'" in sql


def test_a_freeze_resumes_after_the_largest_frozen_id_through_the_parent() -> None:
    sql, params = last_frozen_customer_query("m1", "b1")
    assert "JOIN crm_broadcast b ON b.id = r.broadcast_id" in sql
    assert "WHERE b.merchant_id = $1 AND r.broadcast_id = $2" in sql
    assert "ORDER BY r.customer_id DESC" in sql and "LIMIT 1" in sql
    assert params == ["m1", "b1"]


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
    # One count back, never the cancelled ids (a failed 2-lakh freeze).
    assert "RETURNING 1" in cancel
    assert "SELECT count(*) AS cancelled FROM cancelled" in cancel
    assert "RETURNING customer_id" not in cancel


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
        # The grace: a "now" that took its request latency to arrive.
        ("live", NOW - timedelta(seconds=60), []),
        ("live", NOW - timedelta(seconds=61), ["scheduled_for: is in the past"]),
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


async def test_a_moment_inside_the_grace_is_aimed_and_due_at_once(world) -> None:
    w = world(_broadcast("draft"))
    just_now = datetime.now(timezone.utc) - timedelta(seconds=30)
    aimed = await broadcasts.schedule_broadcast("m1", str(w.broadcast.id), just_now)
    # Stored as sent, already past: the claim's scheduled_for <= now() takes
    # it at once.
    assert aimed.status == "scheduled" and aimed.scheduled_for == just_now


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
    w = world(_broadcast("resolving", resolved_at=NOW, attempts=1))
    w.audience = _people(2500)
    await broadcasts.freeze_broadcast(w.broadcast)
    assert len(w.recipients) == 2500 and set(w.recipients.values()) == {"queued"}
    assert w.pages_read == 3
    assert w.broadcast.status == "dispatching"
    assert w.broadcast.dispatched_at is None  # fired is phase 3's


async def test_every_page_renews_the_lease_in_its_own_atom(world) -> None:
    w = world(_broadcast("resolving", attempts=1))
    w.audience = _people(2500)
    await broadcasts.freeze_broadcast(w.broadcast)
    assert w.writes == [
        (f"txn-{page}", write) for page in (1, 2, 3) for write in ("insert", "renew")
    ]


async def test_an_empty_segment_dispatches_nobody(world) -> None:
    w = world(_broadcast("resolving"))
    await broadcasts.freeze_broadcast(w.broadcast)
    assert w.recipients == {} and w.broadcast.status == "dispatching"


async def test_a_taken_over_freeze_resumes_and_adds_no_one_twice(world) -> None:
    w = world(_broadcast("resolving", attempts=2))
    w.audience = _people(1500)
    w.recipients = {c: "queued" for c in w.audience[:1000]}  # the dead worker's page
    await broadcasts.freeze_broadcast(w.broadcast)
    assert len(w.recipients) == 1500
    # Resumed after the last queued id: page one is never read again.
    assert w.afters == [w.audience[999]] and w.pages_read == 1
    assert w.broadcast.status == "dispatching"


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
    w = world(_broadcast("resolving", attempts=2))
    w.audience = _people(1500)
    w.recipients = {c: "queued" for c in w.audience[:1000]}  # an earlier attempt
    w.unanswerable = error
    await broadcasts.freeze_broadcast(w.broadcast)
    assert w.broadcast.status == "failed"
    assert w.broadcast.last_error == f"the segment cannot be answered: {error}"
    # It will never dispatch: its queue dies with it, in the same atom.
    assert set(w.recipients.values()) == {"cancelled"}
    assert w.writes == [("txn-1", "cancel_queue"), ("txn-1", "fail")]


async def test_any_other_error_is_kept_and_raised_for_the_next_claim(world) -> None:
    w = world(_broadcast("resolving", attempts=1))
    w.audience = _people(2500)
    w.broken = TimeoutError("replica read timed out")
    with pytest.raises(TimeoutError):
        await broadcasts.freeze_broadcast(w.broadcast)
    assert w.broadcast.status == "resolving"  # retried when the lease runs out
    assert w.broadcast.last_error == "TimeoutError: replica read timed out"
    assert len(w.recipients) == 1000  # page one stays queued for the resume


async def test_a_long_error_is_kept_short(world) -> None:
    w = world(_broadcast("resolving", attempts=1))
    w.audience = _people(2500)
    w.broken = RuntimeError("x" * 5000)
    with pytest.raises(RuntimeError):
        await broadcasts.freeze_broadcast(w.broadcast)
    assert len(w.broadcast.last_error or "") == broadcasts.LAST_ERROR_CHARS


async def test_a_broadcast_past_its_attempts_fails_with_the_last_error(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broadcasts, "CRM_BROADCAST_FREEZE_MAX_ATTEMPTS", 3)
    w = world(_broadcast("resolving", attempts=4, last_error="TimeoutError: slow"))
    w.audience = _people(10)
    w.recipients = {c: "queued" for c in w.audience[:4]}
    await broadcasts.freeze_broadcast(w.broadcast)
    assert w.broadcast.status == "failed" and w.pages_read == 0
    assert w.broadcast.last_error == "gave up after 3 attempts: TimeoutError: slow"
    assert set(w.recipients.values()) == {"cancelled"}


async def test_the_last_allowed_attempts_error_fails_it_at_once(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not one lease later: the error of attempt N is the verdict."""
    monkeypatch.setattr(broadcasts, "CRM_BROADCAST_FREEZE_MAX_ATTEMPTS", 3)
    w = world(_broadcast("resolving", attempts=3))
    w.audience = _people(2500)
    w.broken = TimeoutError("replica read timed out")
    await broadcasts.freeze_broadcast(w.broadcast)  # not raised: it is decided
    assert w.broadcast.status == "failed"
    assert (
        w.broadcast.last_error
        == "gave up after 3 attempts: TimeoutError: replica read timed out"
    )
    assert len(w.recipients) == 1000  # page one landed, then its queue died
    assert set(w.recipients.values()) == {"cancelled"}


async def test_an_attempt_before_the_last_keeps_its_error_and_raises(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broadcasts, "CRM_BROADCAST_FREEZE_MAX_ATTEMPTS", 3)
    w = world(_broadcast("resolving", attempts=2))
    w.audience = _people(2500)
    w.broken = TimeoutError("replica read timed out")
    with pytest.raises(TimeoutError):
        await broadcasts.freeze_broadcast(w.broadcast)
    assert w.broadcast.status == "resolving"
    assert set(w.recipients.values()) == {"queued"}


async def test_a_fail_that_finds_the_broadcast_cancelled_changes_nothing(
    world,
) -> None:
    w = world(_broadcast("cancelled"))
    w.recipients = {"c-1": "cancelled", "c-2": "enrolled"}
    failed = await broadcasts.atomically(
        broadcasts._fail_freeze_in_txn, "m1", str(w.broadcast.id), "why"
    )
    assert failed is None and w.broadcast.status == "cancelled"
    assert w.broadcast.last_error is None and w.writes == []


async def test_the_last_allowed_attempt_still_freezes(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broadcasts, "CRM_BROADCAST_FREEZE_MAX_ATTEMPTS", 3)
    w = world(_broadcast("resolving", attempts=3, last_error="TimeoutError: slow"))
    w.audience = _people(10)
    await broadcasts.freeze_broadcast(w.broadcast)
    assert w.broadcast.status == "dispatching" and len(w.recipients) == 10
    # The retry worked: the console shows no stale error.
    assert w.broadcast.attempts == 0 and w.broadcast.last_error is None


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


def _loops(monkeypatch: pytest.MonkeyPatch, role: str) -> List[Dict[str, Any]]:
    loops: List[Dict[str, Any]] = []

    async def drain(claim: Any, handle: Any, **kw: Any) -> None:
        loops.append({"claim": claim, "handle": handle, **kw})

    monkeypatch.setattr(worker_main, "run_drain_loop", drain)
    asyncio.run(worker_main.ROLES[role](asyncio.Event()))
    return loops


def test_the_walker_role_is_the_walkers_one_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # design/worker-runtime.md: one role per pod, no pod runs two loops.
    (walker,) = _loops(monkeypatch, "walker")
    assert walker["name"] == "walker"
    assert walker["claim"] is worker_main.claim_due_runs
    assert walker["handle"] is worker_main.walk_run


def test_the_broadcaster_is_its_own_role_one_broadcast_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (broadcaster,) = _loops(monkeypatch, "broadcaster")
    assert broadcaster["name"] == "broadcaster"
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

"""Broadcasts phase 4 — pause and resume, the report (the funnel), and close
(app/crm/outreach/broadcasts.py, broadcast_report.py, analytics.calls_of_runs).

The story: Flipkart's broadcast B7 ("Offered, not granted" into the "Festive
offer call" plan, max_age_days 3) froze 2,600 people and fired on 20 Oct at
10:00.

What is proven here (no database — the PR's end-to-end run is on Postgres):
- the SQL: pause/resume move one status to the next only from the one
  named; the close job reads only sent broadcasts at least a day past the
  fire (or never fired) and closes only from sent; the report's reads are
  merchant-first — every value a parameter;
- pause and resume: the transitions, and 404 / 409 with where it stands;
- the window: the last run's whole life plus a day; none before any run;
- the close job: a broadcast that admitted nobody closes at once; one not a
  day past its window is skipped without reading its runs; a pause that
  admitted the rest late keeps it open; a gone workflow uses the default
  life; a race lost to another pod is not counted;
- the report: the counts folded (frozen, by status, skips by reason, runs
  open and ended, calls), closes_at from the last run's entry;
- the calls read: the same fold as the plan's calls summary, in batches,
  summed across batches; one read when no batch is asked (the Performance
  tab unchanged); no read for no runs;
- the routes: pause, resume and the funnel answer 200 / 404 / 409.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.crm.outreach.analytics as analytics
import app.crm.outreach.broadcast_report as report
import app.crm.outreach.broadcasts as broadcasts
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.outreach import api as outreach_api
from app.crm.outreach.db.queries.broadcast import (
    close_broadcast_query,
    close_candidates_query,
    move_broadcast_query,
)
from app.crm.outreach.db.queries.broadcast_recipient import recipient_counts_query
from app.crm.outreach.db.queries.enrollment import (
    broadcast_run_endings_query,
    last_broadcast_entry_query,
)
from app.crm.outreach.schemas import Broadcast, RunEnding

NOW = datetime.now(timezone.utc)
FIRED = datetime(2026, 10, 20, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
HOSTILE = "x'); DROP TABLE crm_broadcast; --"
B7 = UUID(int=77)
PLAN = {
    "entry": {"topic": "OFFERED", "reenter": True, "cooldown_hours": 24},
    "goals": [{"topics": ["GRANTED"], "exit_reason": "goal_met"}],
    "exits": {"max_age_days": 3},
    "nodes": [{"id": "call-1", "type": "call", "template_id": "festive"}],
    "edges": [],
}


def _broadcast(status: str = "dispatching", **kw: Any) -> Broadcast:
    return Broadcast(
        id=kw.pop("id", B7),
        merchant_id="m1",
        name="Offered, not granted",
        segment_id=uuid4(),
        workflow_id=UUID(int=88),
        status=status,  # type: ignore[arg-type]
        created_at=FIRED - timedelta(days=1),
        **kw,
    )


# --- the SQL ---


def test_pause_and_resume_move_only_from_the_status_named() -> None:
    query, values = move_broadcast_query("m1", HOSTILE, "dispatching", "paused")
    assert "WHERE merchant_id = $1 AND id = $2 AND status = $3" in query
    assert "SET status = $4" in query
    assert values == ["m1", HOSTILE, "dispatching", "paused"]
    assert HOSTILE not in query


def test_the_close_job_reads_sent_broadcasts_a_day_past_the_fire() -> None:
    query, values = close_candidates_query(500)
    assert "status = 'sent'" in query
    assert "dispatched_at IS NULL" in query
    assert "dispatched_at <= now() - make_interval(days => 1)" in query
    assert "LIMIT $1" in query and values == [500]


def test_a_broadcast_closes_once_only_from_sent() -> None:
    query, values = close_broadcast_query("m1", HOSTILE)
    assert "SET status = 'closed', closed_at = now()" in query
    assert "WHERE merchant_id = $1 AND id = $2 AND status = 'sent'" in query
    assert values == ["m1", HOSTILE] and HOSTILE not in query


@pytest.mark.parametrize(
    "built",
    [
        recipient_counts_query("m1", HOSTILE),
        broadcast_run_endings_query("m1", HOSTILE),
        last_broadcast_entry_query("m1", HOSTILE),
    ],
)
def test_every_report_read_is_merchant_first_and_parameterised(
    built: Tuple[str, List[Any]],
) -> None:
    query, values = built
    assert values == ["m1", HOSTILE] and HOSTILE not in query
    assert "merchant_id = $1" in query


def test_the_report_reads_the_list_through_its_parent_and_runs_by_source() -> None:
    counts, _ = recipient_counts_query("m1", "b7")
    assert "JOIN crm_broadcast b ON b.id = r.broadcast_id" in counts
    assert "GROUP BY r.status, r.skip_reason" in counts
    runs, _ = broadcast_run_endings_query("m1", "b7")
    assert "source_broadcast_id = $2" in runs


# --- pause and resume ---


class Moves:
    """The broadcast accessor: one broadcast whose status moves."""

    def __init__(self, status: Optional[str]) -> None:
        self.status = status

    async def move_broadcast(
        self, merchant_id: str, bid: str, frm: str, to: str
    ) -> Optional[Broadcast]:
        if self.status != frm or bid != str(B7):
            return None
        self.status = to
        return _broadcast(to)

    async def get_broadcast(self, merchant_id: str, bid: str) -> Optional[Broadcast]:
        if self.status is None or bid != str(B7):
            return None
        return _broadcast(self.status)


async def test_pause_holds_a_dispatching_broadcast_and_resume_frees_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moves = Moves("dispatching")
    monkeypatch.setattr(broadcasts, "broadcast_accessor", moves)
    assert (await broadcasts.pause_broadcast("m1", str(B7))).status == "paused"
    assert (await broadcasts.resume_broadcast("m1", str(B7))).status == "dispatching"


@pytest.mark.parametrize(
    "status, action",
    [
        ("scheduled", broadcasts.pause_broadcast),
        ("paused", broadcasts.pause_broadcast),
        ("sent", broadcasts.pause_broadcast),
        ("dispatching", broadcasts.resume_broadcast),
        ("cancelled", broadcasts.resume_broadcast),
    ],
)
async def test_a_move_from_the_wrong_status_says_where_it_is(
    monkeypatch: pytest.MonkeyPatch, status: str, action: Any
) -> None:
    monkeypatch.setattr(broadcasts, "broadcast_accessor", Moves(status))
    with pytest.raises(broadcasts.BroadcastStateConflict) as raised:
        await action("m1", str(B7))
    assert raised.value.status == status


async def test_another_merchants_broadcast_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broadcasts, "broadcast_accessor", Moves(None))
    with pytest.raises(broadcasts.BroadcastNotFound):
        await broadcasts.pause_broadcast("m1", str(uuid4()))


# --- the reporting window ---


def test_the_window_is_the_last_runs_life_plus_a_day() -> None:
    assert broadcasts.report_window_end(None, 3) is None
    assert broadcasts.report_window_end(FIRED, 3) == FIRED + timedelta(days=4)
    assert broadcasts.report_window_end(FIRED, 0.5) == FIRED + timedelta(hours=36)


class Closer:
    """The accessors the close job reads and writes."""

    def __init__(self, candidates: List[Broadcast]) -> None:
        self.candidates = candidates
        self.last_entry: Dict[str, Optional[datetime]] = {}
        self.entries_read: List[str] = []
        self.closed: List[str] = []
        self.lost: set = set()
        self.plan: Optional[Dict[str, Any]] = PLAN

    async def close_candidates(self, limit: int) -> List[Broadcast]:
        return self.candidates

    async def close_broadcast(self, merchant_id: str, bid: str) -> Any:
        if bid in self.lost:
            return None  # another pod closed it first
        self.closed.append(bid)
        return SimpleNamespace(status="closed")

    async def last_broadcast_entry(
        self, merchant_id: str, bid: str
    ) -> Optional[datetime]:
        self.entries_read.append(bid)
        return self.last_entry.get(bid)

    async def get_workflow(self, merchant_id: str, wid: str) -> Any:
        if self.plan is None:
            return None
        return SimpleNamespace(status="live", definition=self.plan)


@pytest.fixture
def closer(monkeypatch: pytest.MonkeyPatch):
    def _install(candidates: List[Broadcast]) -> Closer:
        c = Closer(candidates)
        monkeypatch.setattr(broadcasts, "broadcast_accessor", c)
        monkeypatch.setattr(broadcasts, "enrollment_accessor", c)
        monkeypatch.setattr(broadcasts, "workflow_accessor", c)
        return c

    return _install


async def test_the_close_job_closes_only_broadcasts_whose_window_ended(
    closer,
) -> None:
    nobody = _broadcast("sent", id=UUID(int=1), dispatched_at=None)
    young = _broadcast("sent", id=UUID(int=2), dispatched_at=NOW - timedelta(days=2))
    done = _broadcast("sent", id=UUID(int=3), dispatched_at=NOW - timedelta(days=5))
    paused_long = _broadcast(
        "sent", id=UUID(int=4), dispatched_at=NOW - timedelta(days=5)
    )
    c = closer([nobody, young, done, paused_long])
    c.last_entry = {
        str(done.id): NOW - timedelta(days=4, hours=23),  # its last run entered then
        str(paused_long.id): NOW - timedelta(days=1),  # the rest admitted yesterday
    }
    assert await broadcasts.close_due_broadcasts() == 2
    assert c.closed == [str(nobody.id), str(done.id)]
    # max_age 3 + a day: two days after the fire is too early, decided
    # without reading the runs at all.
    assert str(young.id) not in c.entries_read


async def test_a_gone_workflow_uses_the_default_run_life(closer) -> None:
    # The model's default is 7 days: 6 days after the fire is not yet final.
    six_days = _broadcast("sent", dispatched_at=NOW - timedelta(days=6))
    c = closer([six_days])
    c.plan = None
    assert await broadcasts.close_due_broadcasts() == 0
    c.candidates = [_broadcast("sent", dispatched_at=NOW - timedelta(days=9))]
    assert await broadcasts.close_due_broadcasts() == 1


async def test_one_broadcast_the_job_cannot_judge_never_stops_the_rest(
    closer,
) -> None:
    broken = _broadcast("sent", id=UUID(int=8), dispatched_at=NOW - timedelta(days=9))
    nobody = _broadcast("sent", id=UUID(int=9), dispatched_at=None)
    c = closer([broken, nobody])
    c.plan = {"nodes": "not a plan"}  # a stored document that will not validate
    assert await broadcasts.close_due_broadcasts() == 1
    assert c.closed == [str(nobody.id)]


async def test_a_close_another_pod_won_is_not_counted(closer) -> None:
    nobody = _broadcast("sent", dispatched_at=None)
    c = closer([nobody])
    c.lost = {str(nobody.id)}
    assert await broadcasts.close_due_broadcasts() == 0 and c.closed == []


# --- the report ---


def _ending(n: int, status: str, reason: Optional[str], entered: datetime) -> RunEnding:
    return RunEnding(
        str(UUID(int=1000 + n)),
        str(UUID(int=n)),
        status,
        reason,
        None if status != "exited" else entered + timedelta(hours=1),
        entered,
        "call-1",
    )


ENDINGS = [
    _ending(1, "waiting", None, FIRED),
    _ending(2, "parked", None, FIRED),
    _ending(3, "exited", "goal_met", FIRED),
    _ending(4, "exited", "timed_out", FIRED + timedelta(minutes=2)),
    _ending(5, "exited", "timed_out", FIRED + timedelta(minutes=1)),
]
COUNTS: Dict[Tuple[str, Optional[str]], int] = {
    ("enrolled", None): 2395,
    ("skipped", "no_phone"): 100,
    ("skipped", "already_in_flow"): 50,
    ("skipped", "cooldown_active"): 50,
    ("skipped", "customer_gone"): 5,
}
CALLS = analytics.summarize_calls(
    {
        "outcomes": [
            {"template": "festive", "outcome": "COMPLETED", "spoke": True, "calls": 3}
        ],
        "contacted": 3,
        "reached": 2,
    }
)


def test_the_funnel_folds_the_list_the_runs_and_the_calls() -> None:
    funnel = report.build_funnel(
        _broadcast("sent", dispatched_at=FIRED), COUNTS, ENDINGS, CALLS, 3, NOW
    )
    assert funnel.recipients.frozen == 2600
    assert funnel.recipients.by_status == {"enrolled": 2395, "skipped": 205}
    assert funnel.recipients.skipped_by_reason == {
        "no_phone": 100,
        "already_in_flow": 50,
        "cooldown_active": 50,
        "customer_gone": 5,
    }
    assert funnel.runs.started == 5
    assert funnel.runs.open == {"waiting": 1, "parked": 1}
    assert funnel.runs.by_exit_reason == {"goal_met": 1, "timed_out": 2}
    assert funnel.calls.placed == 3 and funnel.computed_at == NOW
    # Final a run's life (3 days) plus a day after the LAST run entered.
    assert funnel.closes_at == FIRED + timedelta(minutes=2, days=4)


def test_before_it_fires_the_funnel_has_no_close_time() -> None:
    funnel = report.build_funnel(
        _broadcast("dispatching"), {("queued", None): 2600}, [], CALLS, 3, NOW
    )
    assert funnel.recipients.by_status == {"queued": 2600}
    assert funnel.runs.started == 0 and funnel.closes_at is None


async def test_the_calls_read_is_batched_and_summed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batches: List[List[str]] = []

    async def stats(merchant_id: str, runs: List[Any]) -> List[Dict[str, Any]]:
        batches.append([r[0] for r in runs])
        return [
            {
                "template": "festive",
                "outcome": "COMPLETED",
                "spoke": True,
                "calls": len(runs),
            }
        ]

    async def facts(merchant_id: str, runs: List[Any]) -> Dict[str, Any]:
        return {r[0]: [{"placed": 1, "answered": 1}] for r in runs}

    monkeypatch.setattr(analytics, "get_call_stats_by_runs", stats)
    monkeypatch.setattr(analytics, "get_call_facts_by_runs", facts)
    summary = await analytics.calls_of_runs("m1", ENDINGS, batch=2)
    assert [len(b) for b in batches] == [2, 2, 1]
    assert summary.placed == 5 and summary.by_outcome == {"COMPLETED": 5}
    assert summary.contacted_runs == 5 and summary.reached_runs == 5
    batches.clear()
    await analytics.calls_of_runs("m1", ENDINGS)  # the Performance tab: one read
    assert [len(b) for b in batches] == [5]
    batches.clear()
    empty = await analytics.calls_of_runs("m1", [])
    assert batches == [] and empty.placed == 0


# --- the routes ---


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(outreach_api.broadcast_router, prefix="/broadcasts")
    app.dependency_overrides[get_current_user_with_rbac] = lambda: SimpleNamespace(
        role="user", username="u", merchant_ids=["m1"], reseller_ids=[], email="ops@x"
    )
    return TestClient(app)


def test_the_pause_and_resume_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broadcasts, "broadcast_accessor", Moves("dispatching"))
    pause = f"/broadcasts/{B7}/pause?merchant_id=m1"
    resume = f"/broadcasts/{B7}/resume?merchant_id=m1"
    paused = _client().post(pause)
    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    again = _client().post(pause)
    assert (
        again.status_code == 409 and "only a dispatching one" in again.json()["detail"]
    )
    assert _client().post(resume).json()["status"] == "dispatching"
    not_paused = _client().post(resume)
    assert not_paused.status_code == 409
    assert "only a paused one" in not_paused.json()["detail"]
    missing = f"/broadcasts/{uuid4()}/pause?merchant_id=m1"
    assert _client().post(missing).status_code == 404


def test_the_funnel_route_reads_through_the_real_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_broadcast(merchant_id: str, bid: str) -> Optional[Broadcast]:
        return _broadcast("sent", dispatched_at=FIRED) if bid == str(B7) else None

    async def counts(merchant_id: str, bid: str) -> Dict[Any, int]:
        return COUNTS

    async def endings(merchant_id: str, bid: str) -> List[RunEnding]:
        return ENDINGS

    async def no_calls(merchant_id: str, runs: List[Any]) -> Any:
        return [] if isinstance(runs, list) else {}

    async def no_facts(merchant_id: str, runs: List[Any]) -> Dict[str, Any]:
        return {}

    async def workflow(merchant_id: str, wid: str) -> Any:
        return SimpleNamespace(status="live", definition=PLAN)

    monkeypatch.setattr(report.broadcast_accessor, "get_broadcast", get_broadcast)
    monkeypatch.setattr(report.recipient_accessor, "recipient_counts", counts)
    monkeypatch.setattr(report.enrollment_accessor, "broadcast_run_endings", endings)
    monkeypatch.setattr(analytics, "get_call_stats_by_runs", no_calls)
    monkeypatch.setattr(analytics, "get_call_facts_by_runs", no_facts)
    monkeypatch.setattr(broadcasts.workflow_accessor, "get_workflow", workflow)
    got = _client().get(f"/broadcasts/{B7}/funnel?merchant_id=m1")
    assert got.status_code == 200
    body = got.json()
    assert body["recipients"]["frozen"] == 2600
    assert body["runs"]["by_exit_reason"] == {"goal_met": 1, "timed_out": 2}
    assert body["calls"]["placed"] == 0
    missing = _client().get(f"/broadcasts/{uuid4()}/funnel?merchant_id=m1")
    assert missing.status_code == 404

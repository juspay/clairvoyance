"""Broadcasts phase 3 — admission (app/crm/outreach/admission.py): every
queued recipient becomes a run of the workflow, stamped source_broadcast_id,
or a skipped row with its reason (canon/06-outreach.md:41-43, T18).

The story: Flipkart's "Offered, not granted" broadcast B7 into the "Festive
offer call" plan (re-entry allowed, 24 h cooldown, a call square). Its
frozen recipients:
- Priya: new, has a phone -> enrolled;
- Ravi: a run of this plan still open -> skipped already_in_flow;
- Kiran: last run began 20 h ago -> skipped cooldown_active;
- Sana: no phone, and the plan calls -> skipped no_phone;
- Dev: merged into Arjun after the freeze -> Arjun's run, enrolled;
- Gone: erased -> skipped customer_gone.

What is proven here (no database — the PR's end-to-end run is on Postgres):
- decide (PURE): each of the six, a keyed plan, a plan that never dials;
- the SQL: the page claim (SKIP LOCKED, key order, who and when), the
  settle (only from claimed), the hold, the reaper, the candidates (paused
  plans hold, a drained broadcast finishes), the share lock, fired once,
  sent only when nothing waits, the run carrying source_broadcast_id — every
  value a parameter;
- a page: one atom per person through the real enrol_step — the run and
  the row's end in the same atom, survivors enrolled, jitter in range, the
  first page fires the broadcast, a short page finishes it;
- a cancel mid-page cancels the rest; a paused plan holds the page; an
  archived one skips it; a race on the open-run unique, a poison row and a
  row the reaper released each end the right way;
- the broadcaster's claim: the reaper, then a due freeze, else admission;
- the schedule refuses a keyed plan and a fact a broadcast run never has.
"""

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import pytest

import app.crm.outreach.admission as admission
import app.crm.outreach.broadcasts as broadcasts
import app.crm.outreach.enrol as enrol_mod
import app.crm.outreach.workers as workers
from app.crm.outreach.catalog_laws import BROADCAST_FACTS, broadcast_problems
from app.crm.outreach.db import UniqueViolation
from app.crm.outreach.db.queries.broadcast import (
    admission_candidates_query,
    finish_dispatch_query,
    mark_fired_query,
    share_lock_broadcast_query,
)
from app.crm.outreach.db.queries.broadcast_recipient import (
    claim_page_query,
    reap_stale_claims_query,
    release_claims_query,
    settle_recipient_query,
)
from app.crm.outreach.schemas import Broadcast, Workflow, WorkflowDefinition

NOW = datetime.now(timezone.utc)
PRIYA, RAVI, KIRAN, SANA, DEV, ARJUN, GONE = (
    str(UUID(int=n)) for n in (1, 2, 3, 4, 5, 6, 7)
)
HOSTILE = "x'); DROP TABLE crm_broadcast; --"

PLAN = {
    "entry": {"topic": "OFFERED", "reenter": True, "cooldown_hours": 24},
    "goals": [{"topics": ["GRANTED"], "exit_reason": "goal_met"}],
    "nodes": [{"id": "call-1", "type": "call", "template_id": "festive"}],
    "edges": [],
}


def _customer(cid: str, **kw: Any) -> SimpleNamespace:
    row = {
        "id": UUID(cid),
        "status": "active",
        "merged_into_id": None,
        "phone": f"+9190000{cid[-5:]}",
        "display_name": None,
    }
    row.update(kw)
    return SimpleNamespace(**row)


CUSTOMERS = {
    PRIYA: _customer(PRIYA, display_name="Priya"),
    RAVI: _customer(RAVI),
    KIRAN: _customer(KIRAN),
    SANA: _customer(SANA, phone=None),
    DEV: _customer(DEV, status="merged_away", merged_into_id=UUID(ARJUN), phone=None),
    ARJUN: _customer(ARJUN, display_name="Arjun"),
    GONE: _customer(GONE, status="erased"),
}
FROZEN = [PRIYA, RAVI, KIRAN, SANA, DEV, GONE]


# --- decide (PURE) ---


def test_each_of_the_six_is_decided_before_any_write() -> None:
    plans = {
        p.customer_id: p
        for p in admission.decide_admissions(
            FROZEN, CUSTOMERS, phone_needed=True, keyed=False
        )
    }
    assert plans[PRIYA].person_id == PRIYA and plans[PRIYA].skip is None
    assert plans[PRIYA].context == {
        "phone": CUSTOMERS[PRIYA].phone,
        "customer_name": "Priya",
    }
    # The guards (Ravi, Kiran) are judged in the atom, not here.
    assert plans[RAVI].skip is None and plans[KIRAN].skip is None
    assert plans[SANA].skip == admission.NO_PHONE
    # Dev's row stays Dev's; the run is the survivor's, with HER phone.
    assert plans[DEV].person_id == ARJUN and plans[DEV].skip is None
    assert plans[DEV].context["phone"] == CUSTOMERS[ARJUN].phone
    assert plans[GONE].skip == admission.CUSTOMER_GONE
    missing = admission.decide_admissions(
        [str(uuid4())], {}, phone_needed=False, keyed=False
    )
    assert missing[0].skip == admission.CUSTOMER_GONE


def test_a_plan_that_never_dials_admits_her_without_a_phone() -> None:
    (sana,) = admission.decide_admissions(
        [SANA], CUSTOMERS, phone_needed=False, keyed=False
    )
    assert sana.skip is None and "phone" not in sana.context


def test_a_keyed_plan_admits_nobody() -> None:
    plans = admission.decide_admissions(
        FROZEN, CUSTOMERS, phone_needed=True, keyed=True
    )
    assert {p.skip for p in plans} == {admission.KEY_REQUIRED}


def test_a_plan_dials_or_messages_when_it_has_a_call_or_send_square() -> None:
    assert admission.needs_phone(WorkflowDefinition.model_validate(PLAN))
    waits = {**PLAN, "nodes": [{"id": "rest", "type": "wait", "minutes": 30}]}
    assert not admission.needs_phone(WorkflowDefinition.model_validate(waits))


def test_the_skip_words_are_the_canons_plus_their_causes() -> None:
    assert admission.SKIP_REASONS[:2] == ("already_in_flow", "cooldown_active")
    assert len(set(admission.SKIP_REASONS)) == len(admission.SKIP_REASONS)


# --- the SQL ---


def _placeholders(sql: str) -> set:
    return {int(n) for n in re.findall(r"\$(\d+)", sql)}


@pytest.mark.parametrize(
    "built",
    [
        claim_page_query(HOSTILE, HOSTILE, 1000),
        settle_recipient_query(HOSTILE, HOSTILE, "skipped", HOSTILE),
        release_claims_query(HOSTILE, [HOSTILE]),
        reap_stale_claims_query(10),
        admission_candidates_query(1),
        share_lock_broadcast_query(HOSTILE, HOSTILE),
        mark_fired_query(HOSTILE, HOSTILE),
        finish_dispatch_query(HOSTILE, HOSTILE),
    ],
)
def test_every_admission_builder_is_parameterised(built: Tuple[str, List[Any]]) -> None:
    sql, params = built
    assert HOSTILE not in sql
    assert _placeholders(sql) == set(range(1, len(params) + 1))


def test_the_page_claim_takes_queued_rows_in_key_order_skip_locked() -> None:
    sql, params = claim_page_query("b7", "pod-1:7", 1000)
    assert "SET status = 'claimed', claimed_by = $2, claimed_at = now()" in sql
    assert "WHERE broadcast_id = $1 AND status = 'queued'" in sql
    assert "ORDER BY customer_id" in sql and "LIMIT $3" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert params == ["b7", "pod-1:7", 1000]


def test_a_row_is_settled_only_from_claimed_and_held_back_to_queued() -> None:
    sql, params = settle_recipient_query("b7", PRIYA, "enrolled", None)
    assert "SET status = $3, skip_reason = $4" in sql  # words bound, never spelled
    assert "AND status = 'claimed'" in sql and params[2:] == ["enrolled", None]
    held, _ = release_claims_query("b7", [PRIYA])
    assert "SET status = 'queued', claimed_by = NULL, claimed_at = NULL" in held
    assert "AND status = 'claimed'" in held


def test_the_reaper_releases_only_claims_older_than_the_lease() -> None:
    sql, params = reap_stale_claims_query(10)
    assert "WHERE status = 'claimed'" in sql
    assert "claimed_at < now() - make_interval(mins => $1)" in sql
    assert "SET status = 'queued', claimed_by = NULL, claimed_at = NULL" in sql
    assert params == [10]


def test_the_candidates_skip_paused_plans_and_keep_a_drained_broadcast() -> None:
    sql, _ = admission_candidates_query(1)
    assert "b.status = 'dispatching' AND w.status <> 'paused'" in sql
    assert "r.status = 'queued'" in sql  # something to admit
    assert (
        "NOT EXISTS" in sql and "r.status IN ('queued', 'claimed')" in sql
    )  # or: finish


def test_fired_once_and_sent_only_when_nothing_waits() -> None:
    fired, _ = mark_fired_query("m1", "b7")
    assert "SET dispatched_at = now()" in fired and "dispatched_at IS NULL" in fired
    sent, _ = finish_dispatch_query("m1", "b7")
    assert "SET status = 'sent'" in sent and "b.status = 'dispatching'" in sent
    assert "NOT EXISTS" in sent and "r.status IN ('queued', 'claimed')" in sent
    lock, _ = share_lock_broadcast_query("m1", "b7")
    assert "FOR SHARE" in lock and "WHERE merchant_id = $1 AND id = $2" in lock


# --- a page, through the real enrol_step ---


class Store:
    """The broadcast, recipient, workflow and enrollment accessors, and
    identity, as one fake store with a txn per atom."""

    def __init__(self, workflow_status: str = "live") -> None:
        self.broadcast_status = "dispatching"
        self.workflow_status = workflow_status
        self.rows: Dict[str, str] = {c: "queued" for c in FROZEN}
        self.reasons: Dict[str, Optional[str]] = {}
        self.runs: List[Dict[str, Any]] = []
        self.fired = 0
        self.sent = False
        self.released: List[str] = []
        self.atoms = 0
        self.writes: List[Tuple[str, str]] = []  # (txn, what)
        self.cancel_after: Optional[str] = None
        self.pause_after: Optional[str] = None
        self.unique_race: set = set()
        self.poison: set = set()
        self.reaped_mid_atom: set = set()

    # broadcast
    async def share_lock_broadcast(self, txn: Any, merchant_id: str, bid: str) -> str:
        return self.broadcast_status

    async def mark_fired(self, merchant_id: str, bid: str) -> None:
        self.fired += 1

    async def finish_dispatch(self, merchant_id: str, bid: str) -> Any:
        if any(s in ("queued", "claimed") for s in self.rows.values()):
            return None
        self.sent = True
        return SimpleNamespace(status="sent")

    async def admission_candidates(self, limit: int) -> List[Any]:
        return [_broadcast()]

    # recipients
    async def claim_page(self, bid: str, who: str, limit: int) -> List[str]:
        page = sorted(c for c, s in self.rows.items() if s == "queued")[:limit]
        self.rows.update({c: "claimed" for c in page})
        return page

    async def settle_recipient(
        self, txn: Any, bid: str, cid: str, status: str, reason: Optional[str]
    ) -> bool:
        if cid in self.reaped_mid_atom or self.rows.get(cid) != "claimed":
            return False
        self.rows[cid], self.reasons[cid] = status, reason
        self.writes.append((txn, f"settle {cid}"))
        if self.cancel_after == cid:
            self.broadcast_status = "cancelled"  # a cancel lands right after her
        if self.pause_after == cid:
            self.broadcast_status = "paused"  # a pause lands right after her
        return True

    async def release_claims(self, bid: str, ids: List[str]) -> None:
        self.released = ids
        self.rows.update({c: "queued" for c in ids})

    # workflow
    async def get_workflow(self, merchant_id: str, wid: str) -> Workflow:
        return Workflow(
            id=UUID(wid),
            merchant_id=merchant_id,
            name="Festive offer call",
            status=self.workflow_status,  # type: ignore[arg-type]
            version=3,
            created_by=None,
            created_at=NOW,
            updated_at=NOW,
            definition=PLAN,
            draft=None,
        )

    # enrollment (what enrol_step reads and writes)
    async def source_event_used(self, txn: Any, *args: Any) -> bool:
        return False

    async def admission_facts(
        self, txn: Any, merchant_id: str, wid: str, ids: List[str], **kw: Any
    ) -> Dict[str, Any]:
        if RAVI in ids:
            return {
                "runs": 1,
                "latest_entered_at": NOW - timedelta(days=3),
                "open_runs": 1,
            }
        if KIRAN in ids:
            return {
                "runs": 1,
                "latest_entered_at": NOW - timedelta(hours=20),
                "open_runs": 0,
            }
        return {"runs": 0, "latest_entered_at": None, "open_runs": 0}

    async def lock_templates_shared(self, *args: Any) -> None:
        return None

    async def insert_enrollment(
        self,
        txn: Any,
        merchant_id: str,
        wid: str,
        version: int,
        customer_id: str,
        node: str,
        wake_at: datetime,
        context: Dict[str, Any],
        key: str,
        source_broadcast_id: Optional[str] = None,
    ) -> Any:
        if customer_id in self.unique_race:
            raise UniqueViolation("open run unique")
        if customer_id in self.poison:
            raise RuntimeError("a bad row")
        self.runs.append(
            {
                "customer": customer_id,
                "version": version,
                "node": node,
                "wake_at": wake_at,
                "context": context,
                "key": key,
                "source_broadcast_id": source_broadcast_id,
            }
        )
        self.writes.append((txn, f"run {customer_id}"))
        return SimpleNamespace(id=uuid4(), current_node=node)


def _broadcast() -> Broadcast:
    return Broadcast(
        id=UUID(int=77),
        merchant_id="flipkart",
        name="Offered, not granted",
        segment_id=uuid4(),
        workflow_id=UUID(int=88),
        status="dispatching",
        created_at=NOW,
    )


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch):
    def _install(**kw: Any) -> Store:
        s = Store(**kw)

        async def _atomically(fn: Any, *args: Any) -> Any:
            s.atoms += 1
            return await fn(f"txn-{s.atoms}", *args)

        async def _customers(merchant_id: str, ids: List[str]) -> Dict[str, Any]:
            return {i: CUSTOMERS[i] for i in ids if i in CUSTOMERS}

        async def _dev_into_arjun(merchant_id: str) -> Dict[str, str]:
            return {DEV: ARJUN}

        for name in ("broadcast_accessor", "recipient_accessor", "workflow_accessor"):
            monkeypatch.setattr(admission, name, s)
        monkeypatch.setattr(admission, "atomically", _atomically)
        monkeypatch.setattr(admission, "get_customers", _customers)
        monkeypatch.setattr(admission, "merged_pairs", _dev_into_arjun)
        monkeypatch.setattr(enrol_mod, "enrollment_accessor", s)
        monkeypatch.setattr(enrol_mod, "version_accessor", s)
        return s

    return _install


async def test_a_page_admits_and_skips_each_with_its_reason(store) -> None:
    s = store()
    before = datetime.now(timezone.utc)
    await admission.admit_page(_broadcast())
    assert s.rows == {
        PRIYA: "enrolled",
        RAVI: "skipped",
        KIRAN: "skipped",
        SANA: "skipped",
        DEV: "enrolled",
        GONE: "skipped",
    }
    assert s.reasons[RAVI] == "already_in_flow"  # her run is still open
    assert s.reasons[KIRAN] == "cooldown_active"
    assert s.reasons[SANA] == "no_phone" and s.reasons[GONE] == "customer_gone"
    by = {run["customer"]: run for run in s.runs}
    assert set(by) == {PRIYA, ARJUN}  # Dev's row, Arjun's run
    for run in by.values():
        assert run["source_broadcast_id"] == str(_broadcast().id)  # the spark
        assert run["key"] == run["customer"] and run["version"] == 3
        assert run["node"] == "call-1"
        # The first wake: now, spread over the jitter (canon T20 col 8).
        assert (
            before
            <= run["wake_at"]
            <= datetime.now(timezone.utc)
            + timedelta(seconds=admission.ADMIT_JITTER_SECONDS)
        )
    assert by[PRIYA]["context"] == {
        "phone": CUSTOMERS[PRIYA].phone,
        "customer_name": "Priya",
    }
    assert by[ARJUN]["context"]["customer_name"] == "Arjun"
    assert s.fired == 1 and s.sent  # a short page is the last: sent


async def test_her_run_and_her_rows_end_share_one_atom(store) -> None:
    s = store()
    await admission.admit_page(_broadcast())
    atom_of = dict((what, txn) for txn, what in s.writes)
    assert atom_of[f"run {PRIYA}"] == atom_of[f"settle {PRIYA}"]
    assert atom_of[f"run {ARJUN}"] == atom_of[f"settle {DEV}"]
    assert len({txn for txn, _ in s.writes}) == len(FROZEN)  # one atom a person


async def test_a_full_page_leaves_the_broadcast_dispatching(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = store()
    monkeypatch.setattr(admission, "ADMIT_PAGE", 2)
    await admission.admit_page(_broadcast())
    assert list(s.rows.values()).count("queued") == 4 and not s.sent


async def test_a_cancel_mid_page_cancels_everyone_after_it(store) -> None:
    s = store()
    s.cancel_after = PRIYA
    await admission.admit_page(_broadcast())
    assert s.rows[PRIYA] == "enrolled"
    assert {s.rows[c] for c in FROZEN if c != PRIYA} == {"cancelled"}
    assert [run["customer"] for run in s.runs] == [PRIYA]


async def test_a_pause_mid_page_queues_everyone_after_it_again(store) -> None:
    # Phase 4: the person in flight finishes; the rest go back to the queue
    # NOW (not after the reaper's lease), nobody else is enrolled or skipped,
    # and a paused broadcast is never sent.
    s = store()
    s.pause_after = PRIYA
    await admission.admit_page(_broadcast())
    assert s.rows[PRIYA] == "enrolled"
    assert {s.rows[c] for c in FROZEN if c != PRIYA} == {"queued"}
    assert sorted(s.released) == sorted(c for c in FROZEN if c != PRIYA)
    assert [run["customer"] for run in s.runs] == [PRIYA] and not s.sent


async def test_a_paused_plan_holds_the_page_and_fires_nothing(store) -> None:
    s = store(workflow_status="paused")
    await admission.admit_page(_broadcast())
    assert set(s.released) == set(FROZEN) and set(s.rows.values()) == {"queued"}
    assert s.fired == 0 and s.runs == []


async def test_an_archived_plan_admits_nobody(store) -> None:
    s = store(workflow_status="archived")
    await admission.admit_page(_broadcast())
    assert set(s.reasons.values()) == {"workflow_not_live"} and s.runs == []


async def test_a_race_on_the_open_run_unique_is_already_in_flow(store) -> None:
    s = store()
    s.unique_race = {PRIYA}
    await admission.admit_page(_broadcast())
    assert s.rows[PRIYA] == "skipped" and s.reasons[PRIYA] == "already_in_flow"


async def test_a_poison_row_is_skipped_once_never_retried_forever(store) -> None:
    s = store()
    s.poison = {PRIYA}
    await admission.admit_page(_broadcast())
    assert s.rows[PRIYA] == "skipped" and s.reasons[PRIYA] == "admission_failed"
    assert s.rows[DEV] == "enrolled"  # the page goes on


async def test_a_row_the_reaper_released_mid_atom_rolls_its_run_back(store) -> None:
    s = store()
    s.reaped_mid_atom = {PRIYA}
    outcome = await admission._admit_one(
        _broadcast(),
        await s.get_workflow("flipkart", str(UUID(int=88))),
        WorkflowDefinition.model_validate(PLAN),
        admission.Admission(PRIYA, PRIYA, {"phone": "+91"}, None),
        [PRIYA],
    )
    # The atom raised: in Postgres its run never commits; nothing is settled.
    assert outcome == "released" and s.rows[PRIYA] == "queued"


async def test_an_empty_page_finishes_the_broadcast(store) -> None:
    s = store()
    s.rows = {}
    await admission.admit_page(_broadcast())
    assert s.sent and s.fired == 0


# --- the broadcaster's claim ---


async def test_the_broadcaster_reaps_then_freezes_first_else_admits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: List[str] = []
    due: List[Any] = []

    async def reap() -> int:
        order.append("reap")
        return 0

    async def close() -> int:
        order.append("close")
        return 0

    async def freezes(batch: int) -> List[Any]:
        order.append("freeze?")
        return due

    async def candidates(batch: int) -> List[Any]:
        order.append("admit?")
        return [_broadcast()]

    monkeypatch.setattr(workers, "release_stale_claims", reap)
    monkeypatch.setattr(workers, "close_due_broadcasts", close)
    monkeypatch.setattr(workers, "claim_due_freezes", freezes)
    monkeypatch.setattr(workers, "admission_candidates", candidates)
    monkeypatch.setattr(workers, "_last_reap_at", float("-inf"))
    (job,) = await workers.claim_broadcaster_work(1)
    assert job.kind == "admit" and order == ["reap", "close", "freeze?", "admit?"]
    due.append(_broadcast())
    (job,) = await workers.claim_broadcaster_work(1)
    assert job.kind == "freeze"  # an audience is frozen before it is admitted
    # The reaper and the close job keep their own interval.
    assert order.count("reap") == 1 and order.count("close") == 1


# --- the schedule refuses what admission could only skip or park ---


def test_the_schedule_refuses_a_keyed_plan() -> None:
    keyed = {**PLAN, "entry": {**PLAN["entry"], "key": "order_id"}}
    (problem,) = broadcast_problems(WorkflowDefinition.model_validate(keyed), None)
    assert "one run per 'order_id'" in problem


def test_the_schedule_refuses_a_fact_a_broadcast_run_never_has() -> None:
    asks = {
        **PLAN,
        "nodes": [
            {
                "id": "nudge",
                "type": "send",
                "channel": "whatsapp",
                "template": "offer",
                "variables": {"1": "customer_name", "2": "cart_value"},
            }
        ],
    }
    problems = broadcast_problems(
        WorkflowDefinition.model_validate(asks), {"OFFERED": {}}
    )
    assert len(problems) == 1 and "'2' <- 'cart_value'" in problems[0]
    assert "customer_name" in BROADCAST_FACTS  # what it does carry


async def test_each_runs_first_wake_is_spread_by_the_jitter(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = store()
    monkeypatch.setattr(admission.random, "uniform", lambda a, b: 42.0)
    before = datetime.now(timezone.utc)
    await admission.admit_page(_broadcast())
    for run in s.runs:  # a call square wakes now — plus her share of the spread
        assert run["wake_at"] - before >= timedelta(seconds=42)


async def test_scheduling_a_keyed_plan_is_refused_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keyed = {**PLAN, "entry": {**PLAN["entry"], "key": "order_id"}}

    async def draft(merchant_id: str, bid: str) -> Any:
        return SimpleNamespace(status="draft", workflow_id=UUID(int=88))

    async def workflow(merchant_id: str, wid: str) -> Any:
        return SimpleNamespace(status="live", definition=keyed)

    async def no_catalogs(merchant_id: str, raw: Any) -> None:
        return None

    async def never(*args: Any) -> None:
        raise AssertionError("a refused broadcast was aimed")

    monkeypatch.setattr(
        broadcasts,
        "broadcast_accessor",
        SimpleNamespace(get_broadcast=draft, schedule_broadcast=never),
    )
    monkeypatch.setattr(
        broadcasts, "workflow_accessor", SimpleNamespace(get_workflow=workflow)
    )
    monkeypatch.setattr(broadcasts, "gather_catalogs", no_catalogs)
    with pytest.raises(broadcasts.BroadcastInvalid) as refused:
        await broadcasts.schedule_broadcast("flipkart", "b7", None)
    assert any("one run per 'order_id'" in p for p in refused.value.problems)

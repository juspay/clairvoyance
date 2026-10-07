"""Break Package C (T9 sweep + reconcilers, T11 switch, T12 wiring) on real Redis.

A number is switched between today's dialler and v2 while calls, tickets, today's workers
and the DB ``channels`` counter keep moving. The DB is a small in-memory model (``FakeDB``)
whose reads can be interleaved with status changes, which is exactly where the races live.
Over-dial, lost leads, double dials and a stuck DB counter are the failures that matter.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from typing import Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch import (
    channel_semaphore as ch_mod,
    leader as leader_mod,
    queue as queue_mod,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    reconcile as RC,
    release as R,
    routes,
    scripts,
    sweep as SW,
    switch as SWT,
)
from app.core.config import dynamic as dyn_cfg
from app.database.accessor.breeze_buddy.dispatch import LeadDispatchState as S
from app.schemas import (
    CallDirection,
    CallProvider,
    TelephonyNumber,
    TelephonyNumberStatus,
)
from app.services.live_config import store as cfg_store
from tests.breeze_buddy.dispatch.v2.conftest import (
    OWNER,
    _Svc,
    claim_next,
    seed_number,
    tickets_of,
    use_redis,
)

pytestmark = pytest.mark.asyncio

T0 = (
    1_000_000_000_000  # the switch job's clock (ms); Lua uses Redis TIME for due checks
)
# The real dynamic-config readers, captured before any test patches them.
_REAL_ENABLED = dyn_cfg.BB_DISPATCH_V2_ENABLED
_REAL_NUMBERS = dyn_cfg.BB_DISPATCH_V2_NUMBERS


def now_ms() -> int:
    return int(time.time() * 1000)


def due(seconds_ago: float = 5) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


def ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def number(n: str = "N1", max_lines: int = 5, **kw) -> TelephonyNumber:
    return TelephonyNumber(
        id=n,
        number=f"+91{n}",
        provider=kw.get("provider", CallProvider.PLIVO),
        status=kw.get("status", TelephonyNumberStatus.AVAILABLE),
        channels=0,
        maximum_channels=max_lines,
    )


# ---------------------------------------------------------------------------
# The DB as switch.py / reconcile.py read it (same row semantics as the queries)
# ---------------------------------------------------------------------------


@dataclass
class Lead:
    id: str
    template_id: Optional[str]
    status: str = "BACKLOG"
    is_locked: bool = False
    number_id: Optional[str] = None  # stamped telephony_number_id (set when dialled)
    direction: str = "OUTBOUND"
    call_id: Optional[str] = None
    next_attempt_at: Optional[datetime] = None


class FakeDB:
    def __init__(self):
        self.leads: Dict[str, Lead] = {}
        self.numbers: Dict[str, TelephonyNumber] = {}
        self.channels_writes: List[Tuple[str, int]] = []
        self.after_live_read = (
            None  # async hook: runs after a get_live_calls_on_number(s) read
        )

    def add(self, *leads: Lead) -> None:
        for lead in leads:
            if lead.next_attempt_at is None:
                lead.next_attempt_at = due()
            self.leads[lead.id] = lead

    def dial(self, lead_id: str, number_id: str, call_id: Optional[str] = None) -> None:
        """The provider accepted the call: PROCESSING, unlocked, stamped on the number."""
        lead = self.leads[lead_id]
        lead.status, lead.is_locked, lead.number_id = "PROCESSING", False, number_id
        lead.call_id = call_id or f"c-{lead_id}"

    def end(self, lead_id: str) -> None:
        self.leads[lead_id].status = "FINISHED"

    def live(self, number_id: str) -> int:
        return sum(
            1
            for l in self.leads.values()
            if l.status == "PROCESSING" and l.number_id == number_id
        )

    # -- accessors ----------------------------------------------------------------------

    async def live_calls(self, number_id):
        rows = [
            (l.id, l.direction, l.call_id)
            for l in self.leads.values()
            if l.status == "PROCESSING" and l.number_id == number_id
        ]
        if self.after_live_read is not None:
            hook, self.after_live_read = self.after_live_read, None
            await hook()
        return rows

    async def live_calls_many(self, number_ids):
        out: Dict[str, List[Tuple[str, str, Optional[str]]]] = {}
        for l in self.leads.values():
            if l.status == "PROCESSING" and l.number_id in number_ids:
                rows = out.setdefault(str(l.number_id), [])
                rows.append((l.id, l.direction, l.call_id))
        if self.after_live_read is not None:
            hook, self.after_live_read = self.after_live_read, None
            await hook()
        return out

    async def legacy_inflight(self, ids):
        ids = set(ids)
        return [
            (l.id, l.template_id, l.is_locked)
            for l in self.leads.values()
            if l.status == "BACKLOG" and (l.is_locked or l.id in ids)
        ]

    async def states(self, ids):
        return {
            i: S(l.status, l.is_locked, l.template_id, l.next_attempt_at)
            for i in ids
            if (l := self.leads.get(i)) is not None
        }

    async def processing_count(self):
        out: Dict[str, int] = {}
        for l in self.leads.values():
            if l.status == "PROCESSING" and l.number_id:
                out[l.number_id] = out.get(l.number_id, 0) + 1
        return out

    async def set_channels(self, number_id, value):
        self.channels_writes.append((number_id, value))
        return True

    async def numbers_by_ids(self, ids):
        return {i: self.numbers[i] for i in ids if i in self.numbers}

    async def finished_inbound(self, call_ids):
        return {
            l.call_id
            for l in self.leads.values()
            if l.direction == "INBOUND"
            and l.call_id in call_ids
            and l.status == "FINISHED"
        }


def _config(monkeypatch, enabled=True, numbers=("N1",)) -> None:
    monkeypatch.setattr(
        dyn_cfg, "BB_DISPATCH_V2_ENABLED", AsyncMock(return_value=enabled)
    )
    monkeypatch.setattr(
        dyn_cfg, "BB_DISPATCH_V2_NUMBERS", AsyncMock(return_value=list(numbers))
    )


@pytest.fixture
async def env(rr, monkeypatch):
    """Real Redis for every v2 module; the DB is a FakeDB; the switch clock is ``clock[0]``."""
    use_redis(monkeypatch, rr, SWT, RC, routes, ch_mod, SW)
    db = FakeDB()
    monkeypatch.setattr(SWT, "get_telephony_numbers_by_ids", db.numbers_by_ids)
    monkeypatch.setattr(
        SWT, "count_processing_by_telephony_number", db.processing_count
    )
    monkeypatch.setattr(SWT, "set_telephony_number_channels", db.set_channels)
    monkeypatch.setattr(SW, "get_telephony_numbers_by_ids", db.numbers_by_ids)
    monkeypatch.setattr(SW, "set_telephony_number_channels", db.set_channels)
    monkeypatch.setattr(RC, "get_live_calls_on_number", db.live_calls)
    # routes live in Redis here, with no DB to re-resolve them from (the strict refresh
    # would refuse to guess): the seeded route is the truth
    monkeypatch.setattr(RC, "invalidate_route", AsyncMock())
    monkeypatch.setattr(RC, "get_live_calls_on_numbers", db.live_calls_many)
    monkeypatch.setattr(RC, "get_legacy_inflight_leads", db.legacy_inflight)
    monkeypatch.setattr(RC, "get_lead_dispatch_states", db.states)
    monkeypatch.setattr(RC, "get_finished_inbound_calls", db.finished_inbound)
    monkeypatch.setattr(RC, "get_due_backlog_page", AsyncMock(return_value=[]))
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(SW, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(R, "v2_seen", AsyncMock(return_value=True))
    RC._backlog_after = None
    RC._missing_last.clear()
    clock = [T0]
    monkeypatch.setattr(SWT, "_now_ms", lambda: clock[0])
    _config(monkeypatch)
    db.numbers["N1"] = number("N1")
    yield NS(r=rr, db=db, clock=clock, mp=monkeypatch)


async def step(env, at_ms: Optional[int] = None) -> None:
    if at_ms is not None:
        env.clock[0] = at_ms
    await SWT.run_switch_step()


async def mode(env, n: str = "N1") -> Optional[str]:
    return await env.r.hget(f"bb:num:{n}", "mode")


async def switch_on(env, n: str = "N1", start_ms: int = T0) -> None:
    """legacy -> v2_pending -> (two stable checks, >= 15 s) -> v2, with a static DB."""
    await step(env, start_ms)
    assert await mode(env, n) == "v2_pending"
    for t in (5_000, 10_000, 15_000):
        await step(env, start_ms + t)
    assert await mode(env, n) == "v2", await env.r.hgetall(f"bb:num:{n}")


async def call_ended(lead_id: str, n: str = "N1", direction=CallDirection.OUTBOUND):
    """Today's call-end handler reaches v2's release for a lead stamped on ``n``."""
    lead = NS(
        id=lead_id,
        telephony_number_id=n,
        call_direction=direction,
        call_id=f"c-{lead_id}",
    )
    return await R.release_lead_line(lead)


def leases(r, n: str = "N1"):
    return r.hkeys(f"bb:inflight:{n}")


async def claimed(n: str) -> Tuple[str, int]:
    """The next ticket on ``n`` (there must be one), as a dial coroutine claims it."""
    ticket = await claim_next(n)
    assert ticket is not None, f"no ticket on {n}"
    return ticket


# ===========================================================================================
# 1. Switch ON with live legacy calls and a legacy worker mid-dispatch
# ===========================================================================================


async def test_seed_misses_a_legacy_dial_that_turns_processing_between_its_two_reads(
    env,
):
    """Break 1 (rule 11 pattern): ``seed_holders`` reads live calls FIRST and locked BACKLOG
    SECOND. A legacy worker that passed the redirect before ``v2_pending`` and whose
    ``update_lead_call_details`` lands between those two reads (BACKLOG+locked ->
    PROCESSING+unlocked) is seen by neither: not in ``bb:busy:N1``, so ``match`` issues one
    ticket too many — three calls on two lines, until the 20 s re-seed happens to catch it
    (the re-seed has the same hole, and the ledger never adds). Reading the locked set first
    closes it: a lead is locked before it is PROCESSING, never both missed."""
    r, db = env.r, env.db
    await seed_number(r, "N1", 2, {"T1": {}}, mode="v2_pending")
    db.add(
        Lead("L", "T1", is_locked=True)
    )  # today's worker holds the lock, in make_call
    await r.zadd("bb:q:T1", {"W1": now_ms() - 2, "W2": now_ms() - 1})

    async def worker_dials():
        db.dial("L", "N1")  # PROCESSING, unlocked, stamped on N1

    db.after_live_read = worker_dials
    await SWT.apply_seed(r, "N1", T0)

    busy = await r.smembers("bb:busy:N1")
    assert "lead:L" in busy, f"the legacy dial is not counted: {busy}"
    assert (
        db.live("N1") + len(await tickets_of(r, "N1")) <= 2
    )  # never more calls than lines


async def test_switch_on_keeps_a_lock_holding_legacy_dial_counted_until_its_call_ends(
    env,
):
    """Break 1 (held): a legacy worker holds a lead's lock for the whole switch-on (greeting
    pre-warm can take 60 s). The signature is stable while it waits, the seed takes the
    locked lead as a holder, v2 issues only the remaining free lines, and when that call is
    dialled and ends it releases through the busy list exactly once."""
    r, db = env.r, env.db
    await r.hset(
        "bb:num:N1", mapping={"max": 3, "provider": "PLIVO", "status": "AVAILABLE"}
    )
    await r.hset(
        "bb:route:T1", mapping={"number": "N1", "enabled": "1", "tier": "normal"}
    )
    await r.sadd("bb:numtpl:N1", "T1")
    db.numbers["N1"] = number("N1", 3)
    db.add(Lead("P1", "T1", status="PROCESSING", number_id="N1"))  # live legacy call
    db.add(Lead("L", "T1", is_locked=True))  # legacy worker, pre-dial, holds the lock
    await switch_on(env)
    for w in ("W1", "W2", "W3"):
        await scripts.enqueue("T1", w, now_ms() - 1)

    assert await r.smembers("bb:busy:N1") == {"lead:P1", "lead:L", "lead:W1"}
    assert await tickets_of(r, "N1") == ["W1"]  # 3 lines - 2 held = 1 ticket

    db.dial("L", "N1")  # the legacy worker's call is placed on the line it was holding
    await step(env, T0 + 20_000)  # re-seed: adds only, nothing doubled
    assert await r.scard("bb:busy:N1") == 3
    assert db.live("N1") + len(await tickets_of(r, "N1")) <= 3

    db.end("L")
    assert await call_ended("L") is True  # released through the busy list (mode v2)
    assert await r.smembers("bb:busy:N1") == {"lead:P1", "lead:W1", "lead:W2"}
    assert await call_ended("L") is False  # a duplicate hang-up frees nothing more
    assert await r.scard("bb:busy:N1") == 3


async def test_switch_on_ignores_todays_ready_list_and_tickets_a_bounced_lead_once(
    env,
):
    """Break 1 (held; expectation revised by the final review's I4): an unlocked lead of N1
    in today's ready list is not part of the signature, since a legacy worker that picks it
    is bounced to its room (only locked leads can be mid-dial). So it does not hold the
    seed back. The bounced lead is then ticketed exactly once (one copy, never also seeded
    as a holder).
    """
    r, db = env.r, env.db
    await r.hset(
        "bb:num:N1", mapping={"max": 2, "provider": "PLIVO", "status": "AVAILABLE"}
    )
    await r.hset(
        "bb:route:T1", mapping={"number": "N1", "enabled": "1", "tier": "normal"}
    )
    await r.sadd("bb:numtpl:N1", "T1")
    db.numbers["N1"] = number("N1", 2)
    db.add(Lead("R1", "T1"))
    await r.rpush("bb:ready:leads", "R1")

    await step(env, T0)
    await step(env, T0 + 5_000)
    await step(env, T0 + 10_000)
    assert await mode(env) == "v2_pending"
    # a legacy worker pops R1, locks it, hits the redirect: unlock + back to its room
    await r.lrem("bb:ready:leads", 0, "R1")
    await scripts.enqueue("T1", "R1", ms(db.leads["R1"].next_attempt_at))
    await step(env, T0 + 15_000)
    assert await mode(env) == "v2"  # the ready list's churn changed nothing
    assert await r.smembers("bb:busy:N1") == {"lead:R1"}  # ticketed, not seeded
    assert await leases(r) == ["R1"]
    assert await r.zcard("bb:q:T1") == 0


# ===========================================================================================
# 2. Switch OFF under load, and the hand-back
# ===========================================================================================


async def test_switch_off_under_load_hands_back_exact_counts_and_loses_no_lead(env):
    """Break 2: tickets out, leases mid-dial, a call ending during the drain, a ticket given
    back as 'not placed', and an enqueue landing inside the hand-back. After the hand-back:
    DB channels == DB PROCESSING count, tokens == max - that, no lead in any room, every
    waiting lead in today's schedule with its score, every v2 key for N1 gone."""
    r, db = env.r, env.db
    await seed_number(r, "N1", 4, {"T1": {}, "T2": {}})
    await r.sadd("bb:v2:active", "N1")
    db.numbers["N1"] = number("N1", 4)
    scores = {f"W{i}": now_ms() - 10 + i for i in range(1, 7)}
    scores["X1"] = now_ms() + 100_000  # due later
    for lead, score in scores.items():
        db.add(Lead(lead, "T2" if lead == "X1" else "T1"))
        await scripts.enqueue("T2" if lead == "X1" else "T1", lead, score)
    assert await tickets_of(r, "N1") == ["W1", "W2", "W3", "W4"]

    tickets = {}
    for _ in range(4):
        lead, tk = await claimed("N1")
        tickets[lead] = tk
    for lead in ("W1", "W2", "W3", "W4"):
        assert (
            await scripts.mark_dialling("N1", lead, tickets[lead], OWNER)
            is scripts.Mark.DIAL
        )
    for lead in ("W1", "W2"):  # placed: the line belongs to the call
        db.dial(lead, "N1")
        assert await scripts.clear_lease("N1", lead, tickets[lead], OWNER)

    _config(env.mp, enabled=False)
    await step(env, T0 + 1_000)
    assert await mode(env) == "draining"
    assert await r.smembers("bb:v2:active") == {"N1"}

    db.end("W1")
    assert await call_ended("W1") is True  # a line frees during the drain ...
    assert len(await tickets_of(r, "N1")) == 0  # ... and no new ticket is issued
    assert await r.zrange("bb:q:T1", 0, -1) == ["W5", "W6"]
    await step(env, T0 + 2_000)
    assert await mode(env) == "draining"  # W3 and W4 still hold leases: wait
    db.dial("W3", "N1")  # W3 placed late
    assert await scripts.clear_lease("N1", "W3", tickets["W3"], OWNER)
    # W4: the provider said "not placed" -> given back; today's worker re-queues it
    assert (
        await scripts.return_line("N1", "W4", tickets["W4"], OWNER, not_placed=True)
        == 0
    )
    assert await queue_mod.schedule_lead(
        "W4", db.leads["W4"].next_attempt_at, template_id="T1"
    )
    assert await r.zcard("bb:q:T1") == 3 and await leases(r) == []

    real_move = scripts.move_room_to_schedule
    raced = []

    async def move(t):
        n = await real_move(t)
        if not raced:
            raced.append(1)
            assert (
                await scripts.enqueue("T1", "LATE", 4_444) == 0
            )  # lands mid hand-back
        return n

    env.mp.setattr(SWT.scripts, "move_room_to_schedule", move)
    await step(env, T0 + 3_000)

    assert await mode(env) == "legacy"
    assert db.channels_writes[-1] == ("N1", 2)  # W2 + W3, the DB's own count
    assert await r.llen("bb:channel:N1") == 2  # 4 - 2
    today = dict(await r.zrange("bb:schedule:leads", 0, -1, withscores=True))
    assert today == {
        "W4": float(ms(db.leads["W4"].next_attempt_at)),
        "W5": float(scores["W5"]),
        "W6": float(scores["W6"]),
        "X1": float(scores["X1"]),
        "LATE": 4_444.0,
    }
    assert not [k async for k in r.scan_iter(match="bb:q:*")]
    for key in ("bb:busy:N1", "bb:inflight:N1"):
        assert not await r.exists(key)
    assert await r.scard("bb:v2:active") == 0
    assert await r.zscore("bb:due", "N1") is None


async def test_handover_flips_the_mode_before_counting_and_writing_todays_gates(env):
    """Ruling C-concern 3: hand-back order is flip mode -> count PROCESSING -> write channels
    + tokens. Today's call-end handler releases the line BEFORE it writes FINISHED
    (managers/calls.py ``handle_call_completion``: "Runs before the completion UPDATE"), so
    with count-first a call ending between the count and the flip is released through the
    busy list, never decrements DB ``channels``, and the written count stays +1 for good.
    With flip-first that call releases through today's path and the count excludes it.
    """
    r, db = env.r, env.db
    await seed_number(r, "N1", 3, {"T1": {}}, mode="draining")
    await r.sadd("bb:v2:active", "N1")
    db.numbers["N1"] = number("N1", 3)
    db.add(Lead("P1", "T1", status="PROCESSING", number_id="N1"))
    order = []
    real_count, real_set = db.processing_count, db.set_channels

    async def count():
        order.append(("count", await mode(env)))
        return await real_count()

    async def set_channels(n, value):
        order.append(("channels", await mode(env), value))
        return await real_set(n, value)

    env.mp.setattr(SWT, "count_processing_by_telephony_number", count)
    env.mp.setattr(SWT, "set_telephony_number_channels", set_channels)
    await SWT.apply_handover(r, "N1", db.numbers["N1"], T0)
    assert order[0] == ("count", "legacy"), order
    assert order[1] == ("channels", "legacy", 1), order
    assert await r.llen("bb:channel:N1") == 2


async def test_handover_moves_a_room_whose_template_first_appeared_during_it(env):
    """Spec §1 step 3: move every room whose route points at N. The hand-back lists the
    templates once, before the first move, and re-uses that list for the second move. A
    template whose first lead is enqueued during the hand-back (its route resolved to N1
    while the mode was still draining) is in neither pass: its room survives on a legacy
    number and nothing matches it until the 300 s orphan prune."""
    r, db = env.r, env.db
    await seed_number(r, "N1", 2, {"T1": {}}, mode="draining")
    await r.sadd("bb:v2:active", "N1")
    await r.zadd("bb:q:T1", {"L1": 1_000})
    real_move = scripts.move_room_to_schedule
    raced = []

    async def move(t):
        n = await real_move(t)
        if not raced:
            raced.append(1)
            # first lead of a new template: ensure_route writes the route, enqueue lands
            await r.hset("bb:route:T9", mapping={"number": "N1", "enabled": "1"})
            await r.sadd("bb:numtpl:N1", "T9")
            assert await scripts.enqueue("T9", "NEW", 9_000) == 0
        return n

    env.mp.setattr(SWT.scripts, "move_room_to_schedule", move)
    await SWT.apply_handover(r, "N1", db.numbers["N1"], T0)
    assert await mode(env) == "legacy"
    assert await r.zscore("bb:schedule:leads", "NEW") == 9_000
    assert not await r.exists("bb:q:T9")


# ===========================================================================================
# 3. Global OFF with many leads on several numbers
# ===========================================================================================


async def test_global_off_drains_every_number_in_one_pass_and_dials_nothing_twice(env):
    """Break 3: three numbers, 1000 waiting leads each, two tickets out per number. One switch
    check turns every number to draining; after the tickets finish every room is drained to
    today's schedule with its score, every lead exactly once, and nothing was taken twice.
    """
    r, db = env.r, env.db
    nums = ("N1", "N2", "N3")
    _config(env.mp, numbers=nums)
    expected: Dict[str, float] = {}
    for n in nums:
        await seed_number(r, n, 2, {f"T{n}": {}})
        await r.sadd("bb:v2:active", n)
        db.numbers[n] = number(n, 2)
        base = now_ms()
        async with r.pipeline(transaction=True) as pipe:
            for i in range(1000):
                lead = f"{n}-L{i}"
                score = base - 10_000 + i if i < 500 else base + 100_000 + i
                expected[lead] = float(score)
                pipe.zadd(f"bb:q:T{n}", {lead: score})
            pipe.zadd("bb:due", {n: base})
            await pipe.execute()
        assert await scripts.match(n) == 2

    taken = []
    for n in nums:
        for _ in range(2):
            lead, tk = await claimed(n)
            taken.append(lead)
            assert await scripts.mark_dialling(n, lead, tk, OWNER) is scripts.Mark.DIAL
            db.add(Lead(lead, f"T{n}"))
            db.dial(lead, n)
            assert await scripts.clear_lease(n, lead, tk, OWNER)
            expected.pop(lead)
    assert len(set(taken)) == 6

    _config(env.mp, enabled=False, numbers=nums)
    await step(env, T0 + 1_000)
    assert [await mode(env, n) for n in nums] == ["draining"] * 3
    for n in nums:
        assert await scripts.match(n) == 0  # a free line issues nothing while draining
    await step(env, T0 + 2_000)
    assert [await mode(env, n) for n in nums] == ["legacy"] * 3

    today = dict(await r.zrange("bb:schedule:leads", 0, -1, withscores=True))
    assert today == expected
    assert not [k async for k in r.scan_iter(match="bb:q:*")]
    assert await r.scard("bb:v2:active") == 0
    for n in nums:
        assert ("%s" % n, 2) in db.channels_writes
        assert await r.llen(f"bb:channel:{n}") == 0  # 2 lines, 2 live calls
        assert await claim_next(n) is None


# ===========================================================================================
# 4. Flapping and config errors
# ===========================================================================================


async def test_flapping_on_off_on_in_both_directions_mid_step(env):
    """Break 4: on -> off while pending (straight hand-back) -> on -> seeded -> off with a
    ticket out -> on again while draining (the drain finishes first, nothing new is issued)
    -> hand-back -> on. Every hand-back leaves no v2 key for N1 and moves its rooms."""
    r, db = env.r, env.db
    await r.hset(
        "bb:route:T1", mapping={"number": "N1", "enabled": "1", "tier": "normal"}
    )
    await r.sadd("bb:numtpl:N1", "T1")
    db.numbers["N1"] = number("N1", 2)
    db.add(Lead("W1", "T1"), Lead("W2", "T1"))

    await step(env, T0)
    assert await mode(env) == "v2_pending"
    assert await scripts.enqueue("T1", "W1", now_ms() - 1) == 0  # room, no ticket
    _config(env.mp, enabled=False)
    await step(env, T0 + 2_000)
    assert await mode(env) == "legacy"  # pending + not desired: straight hand-back
    assert await r.zscore("bb:schedule:leads", "W1") is not None
    assert not await r.exists("bb:q:T1") and await r.scard("bb:v2:active") == 0

    _config(env.mp, enabled=True)
    await r.zrem(
        "bb:schedule:leads", "W1"
    )  # today's promoter picked it ... and bounced it
    await switch_on(env, start_ms=T0 + 4_000)
    assert await scripts.enqueue("T1", "W1", now_ms() - 2) == 1
    assert await scripts.enqueue("T1", "W2", now_ms() - 1) == 1
    assert await tickets_of(r, "N1") == ["W1", "W2"]
    lead, tk = await claimed("N1")
    assert await scripts.mark_dialling("N1", lead, tk, OWNER) is scripts.Mark.DIAL

    _config(env.mp, enabled=False)
    await step(env, T0 + 20_000)
    assert await mode(env) == "draining"
    _config(env.mp, enabled=True)  # desired again mid-drain
    await step(env, T0 + 21_000)
    assert await mode(env) == "draining"  # finish the drain first
    assert await tickets_of(r, "N1") == ["W2"]  # untouched, not re-issued
    lead2, tk2 = await claimed("N1")
    assert (
        await scripts.return_line("N1", lead2, tk2, OWNER) == 0
    )  # given back: no re-issue
    assert await queue_mod.schedule_lead(lead2, due(), template_id="T1")
    db.dial(lead, "N1")
    assert await scripts.clear_lease("N1", lead, tk, OWNER)
    await step(env, T0 + 22_000)
    assert await mode(env) == "legacy"
    assert await r.zscore("bb:schedule:leads", lead2) is not None
    assert db.channels_writes[-1] == ("N1", 1) and await r.llen("bb:channel:N1") == 1
    await step(env, T0 + 23_000)
    assert await mode(env) == "v2_pending"  # ... then on again
    assert await r.smembers("bb:v2:active") == {"N1"}


@pytest.mark.parametrize(
    "failing_key", [None, "BB_DISPATCH_V2_ENABLED", "BB_DISPATCH_V2_NUMBERS"]
)
async def test_a_config_read_error_makes_no_switch_transition(env, failing_key):
    """Ruling C-concern 4: a config read error (flag or number list) -> no switch transition
    this step. ``get_config`` swallows a Redis error and returns the default (False / ""),
    which reads as "global off": one failed read drains every v2 number, then switches it
    back on >= 15 s + a seed later. The step must notice the failure and change nothing.
    (``None`` = the control: both reads succeed through the real readers, nothing moves.)
    """
    r = env.r
    await seed_number(r, "N1", 2, {"T1": {}}, mode="v2")
    await r.sadd("bb:v2:active", "N1")
    env.mp.setattr(dyn_cfg, "BB_DISPATCH_V2_ENABLED", _REAL_ENABLED)
    env.mp.setattr(dyn_cfg, "BB_DISPATCH_V2_NUMBERS", _REAL_NUMBERS)
    env.mp.setattr(cfg_store, "ENABLE_REDIS_DYNAMIC_CONFIG", True)
    env.mp.delenv("BB_DISPATCH_V2_ENABLED", raising=False)
    env.mp.delenv("BB_DISPATCH_V2_NUMBERS", raising=False)
    values = {"BB_DISPATCH_V2_ENABLED": "true", "BB_DISPATCH_V2_NUMBERS": "N1"}

    async def flag(key):
        if key == failing_key:
            raise ConnectionError("redis blip")
        return values[key]

    env.mp.setattr(cfg_store, "_get_flag_from_redis", flag)
    try:
        await SWT.run_switch_step()
    except Exception:  # noqa: BLE001 — raising out is an acceptable "no transition"
        pass
    assert await mode(env) == "v2", "a config read error switched the number off"
    assert await r.smembers("bb:v2:active") == {"N1"}


# ===========================================================================================
# 5. Redis flush while v2 is on
# ===========================================================================================


async def test_redis_flush_restarts_every_desired_number_at_pending_in_one_tick(env):
    """Break 5: epoch missing -> the same tick restarts every desired number at v2_pending
    (no over-dial while the busy lists are empty: match issues nothing), the backlog job
    refills the rooms right after the epoch is set, and the normal switch-on seeds the busy
    list from the DB's live calls before any ticket is issued."""
    r, db = env.r, env.db
    _config(env.mp, numbers=("N1", "N2"))
    for n in ("N1", "N2"):
        await seed_number(r, n, 2, {f"T{n}": {}})
        await r.sadd("bb:v2:active", n)
        db.numbers[n] = number(n, 2)
    db.add(Lead("P1", "TN1", status="PROCESSING", number_id="N1"))  # a live v2 call
    db.add(Lead("W1", "TN1"), Lead("W2", "TN1"), Lead("W3", "TN2"))
    await r.sadd("bb:busy:N1", "lead:P1")
    await r.set("bb:epoch", "old")

    await r.flushdb()  # Redis lost everything

    env.mp.setattr(
        RC,
        "get_due_backlog_page",
        AsyncMock(
            side_effect=[
                [
                    (l, db.leads[l].template_id, db.leads[l].next_attempt_at)
                    for l in ("W1", "W2", "W3")
                ],
                [],
            ]
        ),
    )
    env.mp.setattr(RC, "_is_v2_template", AsyncMock(return_value=True))

    async def resolve(template_id):  # ensure_route after the flush, without a DB
        n = "N1" if template_id == "TN1" else "N2"
        await r.hset(f"bb:route:{template_id}", mapping={"number": n, "enabled": "1"})
        await r.sadd(f"bb:numtpl:{n}", template_id)

    env.mp.setattr(queue_mod, "_ensure_route", resolve)
    env.mp.setattr(SW, "JOBS", (SW.Job("backlog", 60, SW.backlog_job, 5),))
    sw = SW.Sweeper(redis_client=r)
    await sw.tick()
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.gather(*sw._running.values())

    assert await r.exists("bb:epoch")
    assert [await mode(env, n) for n in ("N1", "N2")] == ["v2_pending", "v2_pending"]
    assert await r.smembers("bb:v2:active") == {"N1", "N2"}
    assert await r.hget("bb:num:N1", "max") == "2"
    assert await r.zcard("bb:q:TN1") == 2 and await r.zcard("bb:q:TN2") == 1
    for n in ("N1", "N2"):  # busy lists empty, and still nothing is issued
        assert await scripts.match(n) == 0
        assert await r.scard(f"bb:busy:{n}") == 0 and len(await tickets_of(r, n)) == 0

    for t in (5_000, 10_000, 15_000):
        await step(env, T0 + t)
    assert await mode(env, "N1") == "v2"
    assert await r.smembers("bb:busy:N1") == {
        "lead:P1",
        "lead:W1",
    }  # seeded, then 1 free line
    assert await tickets_of(r, "N1") == ["W1"]
    assert await tickets_of(r, "N2") == ["W3"]


# ===========================================================================================
# 6. Sweeper
# ===========================================================================================


class _LeaderSvc(_Svc):
    async def set(self, key, value, nx=False, ex=None):
        return bool(await self._c.set(key, value, nx=nx, ex=ex))


async def test_two_sweepers_on_real_redis_only_the_leader_ticks(env):
    """Break 6: two sweepers, one leader key: exactly one ticks, and the key is released on
    stop so the other can take over."""
    r = env.r
    svc = _LeaderSvc(r)

    async def _get():
        return svc

    env.mp.setattr(leader_mod, "get_redis_service", _get)
    env.mp.setattr(SW, "SWEEP_INTERVAL_S", 0.02)
    env.mp.setattr(SW, "check_sweep_leader", AsyncMock())
    ticks = {"a": 0, "b": 0}
    sweepers = {}
    for name in ticks:
        sw = SW.Sweeper(redis_client=r)

        async def tick(name=name):
            ticks[name] += 1

        env.mp.setattr(sw, "tick", tick)
        sw.start()
        sweepers[name] = sw
    await asyncio.sleep(0.4)
    leaders = [n for n, sw in sweepers.items() if sw._leader.is_leader]
    assert len(leaders) == 1, leaders
    (leader,) = leaders
    other = "b" if leader == "a" else "a"
    assert ticks[leader] > 0 and ticks[other] == 0, ticks
    assert await r.get("bb:v2:sweep:leader") == sweepers[leader]._leader.instance_id
    await sweepers[leader].stop()
    assert await r.get("bb:v2:sweep:leader") is None  # released, not left to expire
    await sweepers[other].stop()


async def test_tick_keeps_matching_other_numbers_when_one_match_fails(env):
    """A Redis error inside one number's match (None from the script wrapper) must not stop
    the tick from matching the rest."""
    r = env.r
    await r.set("bb:epoch", "x")
    await seed_number(r, "N1", 1, {"T1": {}})
    await seed_number(r, "N2", 1, {"T2": {}})
    await r.zadd("bb:q:T1", {"L1": now_ms() - 1})
    await r.zadd("bb:q:T2", {"L2": now_ms() - 1})
    await r.zadd("bb:due", {"N1": now_ms() - 1, "N2": now_ms() - 1})
    env.mp.setattr(SW, "JOBS", ())
    real_match_many = scripts.match_many

    async def match_many(ids):
        issued = await real_match_many([n for n in ids if n != "N1"])
        return {**issued, "N1": None}

    env.mp.setattr(SW.scripts, "match_many", match_many)
    await SW.Sweeper(redis_client=r).tick()
    assert await tickets_of(r, "N2") == ["L2"]


# ===========================================================================================
# 7. Ledger / reaper / backlog
# ===========================================================================================


async def test_ledger_frees_and_requeues_a_seeded_holder_whose_legacy_dispatch_failed(
    env,
):
    """A lead seeded from today's locked set whose legacy dispatch then failed (deferred and
    unlocked): its re-queue was refused (-2, it still holds a line) so it sits in no room.
    The ledger frees the holder and re-queues it right away (M6) — exactly one copy, one
    ticket — instead of leaving the line held and the lead invisible."""
    r, db = env.r, env.db
    await seed_number(r, "N1", 2, {"T1": {}})
    await r.sadd("bb:v2:active", "N1")
    await r.sadd("bb:busy:N1", "lead:L")  # seeded while locked
    db.add(Lead("L", "T1"))  # now BACKLOG, unlocked, in no room
    assert await scripts.enqueue("T1", "L", ms(db.leads["L"].next_attempt_at)) == -2
    assert await r.zcard("bb:q:T1") == 0

    assert await RC.ledger_check() == {"removed": 1}
    assert await leases(r) == ["L"]  # freed, re-queued and ticketed at once
    assert await r.smembers("bb:busy:N1") == {"lead:L"}
    assert await r.zcard("bb:q:T1") == 0
    assert await tickets_of(r, "N1") == ["L"]


async def test_ledger_does_not_free_a_live_legacy_call_dialled_during_v2(env):
    """A legacy dial that was seeded as a holder and then placed (PROCESSING, unlocked) has
    no lease and no lock, but it is a live call: the ledger must keep its line."""
    r, db = env.r, env.db
    await seed_number(r, "N1", 2, {"T1": {}})
    await r.sadd("bb:v2:active", "N1")
    await r.sadd("bb:busy:N1", "lead:L")
    db.add(Lead("L", "T1"))
    db.dial("L", "N1")
    assert await RC.ledger_check() == {"removed": 0}
    assert await r.smembers("bb:busy:N1") == {"lead:L"}


async def test_backlog_reconciler_never_queues_a_second_copy_of_a_ticketed_lead(env):
    """M2: no Python pre-checks, the Lua decides. A due BACKLOG row that already holds a
    ticket (the dial coroutine has not locked it yet) is not put back in its room."""
    r, db = env.r, env.db
    await seed_number(r, "N1", 2, {"T1": {}})
    await r.sadd("bb:v2:active", "N1")
    db.add(Lead("L", "T1"))
    assert await scripts.enqueue("T1", "L", ms(db.leads["L"].next_attempt_at)) == 1
    env.mp.setattr(
        RC,
        "get_due_backlog_page",
        AsyncMock(side_effect=[[("L", "T1", db.leads["L"].next_attempt_at)], []]),
    )
    env.mp.setattr(RC, "_is_v2_template", AsyncMock(return_value=True))
    assert await RC.reconcile_backlog_v2() == 1  # the Lua answered -2: "holds a line"
    assert await r.zcard("bb:q:T1") == 0
    assert await leases(r) == ["L"] and await tickets_of(r, "N1") == ["L"]


async def test_reaper_frees_a_dead_dial_task_ticket_and_keeps_a_placed_calls_line(env):
    """Rule 14 on real leases: a ticket never marked dialling, older than 180 s, is freed and
    the BACKLOG lead re-queued; a dialling lease stuck 10 min whose lead is PROCESSING loses
    only its lease; a dialling lease stuck 10 min whose lead is still BACKLOG (the provider
    never answered, the task died) frees the line and re-queues."""
    r, db = env.r, env.db
    await seed_number(r, "N1", 3, {"T1": {}})
    await r.sadd("bb:v2:active", "N1")
    db.add(Lead("A", "T1"), Lead("B", "T1"), Lead("C", "T1"))
    for lead in ("A", "B", "C"):
        assert (
            await scripts.enqueue("T1", lead, ms(db.leads[lead].next_attempt_at)) == 1
        )
    tickets = {}
    for _ in range(3):
        lead, tk = await claimed("N1")
        tickets[lead] = tk
    for lead in ("B", "C"):
        assert (
            await scripts.mark_dialling("N1", lead, tickets[lead], OWNER)
            is scripts.Mark.DIAL
        )
    db.dial("B", "N1")
    # age every lease: A never dialled (200 s), B and C dialling for 11 min
    import json

    for lead, age in (("A", 200_000), ("B", 660_000), ("C", 660_000)):
        lease = json.loads(await r.hget("bb:inflight:N1", lead))
        lease["issued_ms"] = lease["claimed_ms"] = now_ms() - age
        if "dialling_ms" in lease:
            lease["dialling_ms"] = now_ms() - age
        await r.hset("bb:inflight:N1", lead, json.dumps(lease))

    assert await RC.reap_leases() == 3
    assert await r.smembers("bb:busy:N1") == {"lead:B", "lead:A", "lead:C"}
    # A and C were re-queued and, with free lines, ticketed again with NEW ticket ids
    new = {l: json.loads(await r.hget("bb:inflight:N1", l))["tk"] for l in ("A", "C")}
    assert new["A"] != tickets["A"] and new["C"] != tickets["C"]
    assert await r.hget("bb:inflight:N1", "B") is None  # B keeps its line, no lease
    assert (
        await scripts.return_line("N1", "A", tickets["A"], OWNER) == -1
    )  # the old ticket is dead

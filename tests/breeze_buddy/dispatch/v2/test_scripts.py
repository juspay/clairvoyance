import asyncio
import datetime as dt
import json
import random
import time
from typing import Optional, Tuple

import pytest

# Importing the dispatch package first initializes worker -> managers.calls in
# the supported order (see tests/breeze_buddy/test_number_picker.py).
import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import redis_client, scripts
from app.ai.voice.agents.breeze_buddy.managers.calls import hours_open
from tests.breeze_buddy.dispatch.v2.conftest import (
    OWNER,
    claim_next,
    seed_number,
    tickets_of,
)

NOW = lambda: int(time.time() * 1000)  # noqa: E731


def _ist_sec() -> int:
    return (int(time.time()) + 19800) % 86400


async def _ticket(rr, n: str) -> Tuple[str, int]:
    """Number ``n``'s oldest ticket, claimed as a dial coroutine claims it."""
    got = await claim_next(n)
    assert got is not None
    return got


@pytest.mark.asyncio
async def test_free_line_gives_ticket_at_once(rr):
    await seed_number(rr, "N1", 2, {"T1": {}})
    assert await scripts.enqueue("T1", "L1", NOW() - 1) == 1
    assert await rr.smembers("bb:busy:N1") == {"lead:L1"}
    assert await tickets_of(rr, "N1") == ["L1"]
    lease = json.loads(await rr.hget("bb:inflight:N1", "L1"))
    assert lease["t"] == "T1" and lease["tk"] == int(await rr.hget("bb:num:N1", "seq"))


@pytest.mark.asyncio
async def test_full_number_waits_then_release_hands_line_to_earliest(rr):
    await seed_number(rr, "N1", 1, {"T1": {}})
    await scripts.enqueue("T1", "L1", NOW() - 3)
    await scripts.enqueue("T1", "L3", NOW() - 1)
    await scripts.enqueue("T1", "L2", NOW() - 2)
    assert await rr.zrange("bb:q:T1", 0, -1) == ["L2", "L3"]  # sorted by due
    assert (await _ticket(rr, "N1"))[0] == "L1"
    assert await scripts.release("N1", "lead:L1") == [1, 1]  # [removed, issued]
    assert await rr.smembers("bb:busy:N1") == {"lead:L2"}  # earliest due won


@pytest.mark.asyncio
async def test_concurrent_enqueues_never_exceed_max(rr):
    await seed_number(rr, "N1", 2, {"T1": {}})
    await asyncio.gather(
        *[scripts.enqueue("T1", f"L{i}", NOW() - 1) for i in range(10)]
    )
    assert await rr.scard("bb:busy:N1") == 2
    assert await rr.zcard("bb:q:T1") == 8


@pytest.mark.asyncio
async def test_release_twice_frees_one_line(rr):
    await seed_number(rr, "N1", 1, {"T1": {}})
    await scripts.enqueue("T1", "L1", NOW() - 1)
    await scripts.enqueue("T1", "L2", NOW() - 1)
    assert await scripts.release("N1", "lead:L1") == [1, 1]
    assert await scripts.release("N1", "lead:L1") == [0, 0]  # duplicate: nothing
    assert await rr.scard("bb:busy:N1") == 1


@pytest.mark.asyncio
async def test_future_lead_not_matched(rr):
    await seed_number(rr, "N1", 2, {"T1": {}})
    due = NOW() + 60_000
    assert await scripts.enqueue("T1", "L1", due) == 0
    assert await rr.zcard("bb:q:T1") == 1
    assert await rr.zscore("bb:due", "N1") == due  # the sweep looks again then


@pytest.mark.asyncio
async def test_tier_beats_earlier_due_and_aging_wins_after_10_min(rr):
    await seed_number(rr, "NS", 1, {"TA": {"tier": "high"}, "TB": {}})
    await scripts.enqueue("TB", "B1", NOW() - 20_000)  # takes the only line
    await scripts.enqueue("TB", "B2", NOW() - 18_000)
    await scripts.enqueue("TA", "A1", NOW() - 1_000)
    await scripts.release("NS", "lead:B1")
    assert await rr.smembers("bb:busy:NS") == {"lead:A1"}  # high tier first
    await scripts.enqueue("TB", "B3", NOW() - 700_000)  # waited > 10 min
    await scripts.enqueue("TA", "A2", NOW() - 1_000)
    await scripts.release("NS", "lead:A1")
    assert await rr.smembers("bb:busy:NS") == {"lead:B3"}  # aging


@pytest.mark.asyncio
async def test_closed_template_paused_reseller_and_kill_switch(rr):
    await seed_number(
        rr, "N1", 3, {"T1": {"enabled": "0"}, "T2": {"reseller": "RP"}, "T3": {}}
    )
    # today's pause key, read by match itself: no mirror to lag behind (Fable M2)
    await rr.set("bb:reseller:paused:RP", "1")
    await scripts.enqueue("T1", "L1", NOW() - 1)
    await scripts.enqueue("T2", "L2", NOW() - 1)
    assert await rr.scard("bb:busy:N1") == 0
    await rr.set("bb:dispatch:enabled", "0")
    await scripts.enqueue("T3", "L3", NOW() - 1)
    assert await rr.scard("bb:busy:N1") == 0
    await rr.set("bb:dispatch:enabled", "1")
    assert await scripts.match("N1") == 1
    await rr.delete("bb:reseller:paused:RP")  # unpaused by hand
    assert await scripts.match("N1") == 1
    assert await rr.smembers("bb:busy:N1") == {"lead:L3", "lead:L2"}


@pytest.mark.asyncio
async def test_match_uses_ist_calling_hours(rr):
    ist = _ist_sec()
    closed = {"start": str((ist + 3600) % 86400), "end": str((ist + 7200) % 86400)}
    open_ = {"start": str((ist - 600) % 86400), "end": str((ist + 600) % 86400)}
    await seed_number(rr, "N1", 2, {"TC": closed, "TO": open_})
    assert await scripts.enqueue("TC", "L1", NOW() - 1) == 0
    assert await scripts.enqueue("TO", "L2", NOW() - 1) == 1
    assert await rr.smembers("bb:busy:N1") == {"lead:L2"}
    # TC waits for its hours: N1 is due when they open
    assert abs(await rr.zscore("bb:due", "N1") - (NOW() + 3_600_000)) < 2_000


@pytest.mark.asyncio
async def test_a_ticket_whose_lease_went_cannot_be_claimed(rr):
    await seed_number(rr, "N1", 1, {"T1": {}})
    await scripts.enqueue("T1", "L1", NOW() - 1)
    await rr.hdel("bb:inflight:N1", "L1")  # lease gone while the ticket waited
    t = scripts.parse_ticket(await rr.lpop("bb:tickets"))
    assert t is not None
    assert await scripts.claim("N1", "L1", t.tk, OWNER) is False


@pytest.mark.asyncio
async def test_enqueue_skips_lead_already_holding_a_line(rr):
    await seed_number(rr, "N1", 2, {"T1": {}})
    await scripts.enqueue("T1", "L1", NOW() - 1)
    assert await scripts.enqueue("T1", "L1", NOW() - 1) == -2  # e.g. reconciler re-add
    assert await rr.zcard("bb:q:T1") == 0 and await rr.scard("bb:busy:N1") == 1


@pytest.mark.asyncio
async def test_enqueue_skips_lead_with_a_lease_but_no_busy_entry(rr):
    await seed_number(rr, "N1", 2, {"T1": {}})
    lease = json.dumps({"t": "T1", "issued_ms": NOW(), "tk": 1})
    await rr.hset("bb:inflight:N1", "L1", lease)
    assert await scripts.enqueue("T1", "L1", NOW() - 1) == -2
    assert await rr.zcard("bb:q:T1") == 0 and await rr.scard("bb:busy:N1") == 0


@pytest.mark.asyncio
async def test_enqueue_relists_template_on_its_number(rr):
    # A lost SADD bb:numtpl:{N} (e.g. a Redis error in the route writer) must not strand
    # T1: a non-empty room always has its template listed on the route's number.
    await seed_number(rr, "N1", 2, {"T1": {}})
    await rr.srem("bb:numtpl:N1", "T1")
    assert await scripts.enqueue("T1", "L1", NOW() - 1) == 1
    assert await rr.sismember("bb:numtpl:N1", "T1")
    assert await rr.smembers("bb:busy:N1") == {"lead:L1"}


@pytest.mark.asyncio
async def test_enqueue_refuses_when_route_missing(rr):
    await seed_number(rr, "N1", 2, {"T1": {}})
    await rr.delete("bb:route:T1")  # e.g. Redis was flushed
    assert await scripts.enqueue("T1", "L1", NOW() - 1) == -1
    assert await rr.zcard("bb:q:T1") == 0
    assert await rr.zscore("bb:due", "N1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "legacy"])
async def test_enqueue_refuses_number_not_v2_accounted(rr, mode):
    # Today's path owns the number: nothing is written, the caller uses today's ZADD.
    await seed_number(rr, "N1", 2, {"T1": {}}, mode=mode)
    assert await scripts.enqueue("T1", "L1", NOW() - 1) == -3
    assert await rr.zcard("bb:q:T1") == 0
    assert await rr.scard("bb:busy:N1") == 0
    assert await rr.zscore("bb:due", "N1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["v2_pending", "draining"])
async def test_pending_and_draining_numbers_queue_without_tickets(rr, mode):
    await seed_number(rr, "N1", 2, {"T1": {}}, mode=mode)
    assert await scripts.enqueue("T1", "L1", NOW() - 1) == 0
    assert await rr.zrange("bb:q:T1", 0, -1) == ["L1"]
    assert await rr.zscore("bb:due", "N1") is not None  # kept for when it is v2
    assert await scripts.match("N1") == 0
    assert await rr.zscore("bb:due", "N1") is not None
    assert await rr.scard("bb:busy:N1") == 0
    await rr.hset("bb:num:N1", "mode", "v2")  # only v2 issues tickets
    assert await scripts.match("N1") == 1
    assert await rr.smembers("bb:busy:N1") == {"lead:L1"}


@pytest.mark.asyncio
async def test_match_drops_template_routed_to_another_number(rr):
    # T1 moved to N2 but is still listed on N1 (e.g. the SREM on the move failed).
    await seed_number(rr, "N1", 2, {"T1": {}})
    await seed_number(rr, "N2", 2, {"T1": {}})
    await rr.zadd("bb:q:T1", {"L1": NOW() - 1})
    assert await scripts.match("N1") == 0
    assert await rr.scard("bb:busy:N1") == 0
    assert not await rr.sismember("bb:numtpl:N1", "T1")  # self-healed
    assert await rr.zscore("bb:due", "N2") <= NOW()  # the next tick matches N2
    assert await rr.zscore("bb:due", "N1") is None  # nothing else waits on N1
    assert await scripts.match("N2") == 1


@pytest.mark.asyncio
async def test_match_prunes_empty_rooms_not_routed_here(rr):
    await seed_number(rr, "N1", 2, {"T1": {}, "T2": {}, "T3": {}})
    await rr.hset("bb:route:T2", "number", "N2")  # moved away
    await rr.delete("bb:route:T3")  # route gone
    assert await scripts.match("N1") == 0
    assert await rr.smembers("bb:numtpl:N1") == {"T1"}  # routed here: kept though empty


@pytest.mark.asyncio
async def test_match_never_reissues_a_lead_that_holds_a_line(rr):
    # L1 is queued under two templates on N1. The second match must drop the duplicate,
    # not overwrite L1's dialling lease with a new ticket (two dials on one busy entry).
    await seed_number(rr, "N1", 2, {"T1": {}, "T2": {}})
    await rr.zadd("bb:q:T1", {"L1": NOW() - 1})
    await rr.zadd("bb:q:T2", {"L1": NOW() + 400, "L2": NOW() + 450})
    assert await scripts.match("N1") == 1
    _, tk = await _ticket(rr, "N1")
    assert await scripts.mark_dialling("N1", "L1", tk, OWNER) is scripts.Mark.DIAL
    await asyncio.sleep(0.5)  # T2's copy of L1, then L2, are due
    assert await scripts.match("N1") == 1  # the duplicate cost no line: L2 got it
    assert await rr.zcard("bb:q:T2") == 0
    assert await tickets_of(rr, "N1") == ["L2"]
    assert json.loads(await rr.hget("bb:inflight:N1", "L1"))["tk"] == tk
    assert await scripts.clear_lease("N1", "L1", tk, OWNER) is True
    assert await rr.smembers("bb:busy:N1") == {"lead:L1", "lead:L2"}


@pytest.mark.asyncio
async def test_unresolved_route_number_is_never_used_as_a_key(rr):
    # The route writer stores number="" when no number resolves.
    await seed_number(rr, "N1", 1, {"T1": {}})
    await scripts.enqueue("T1", "L1", NOW() - 1)
    _, tk = await _ticket(rr, "N1")
    await rr.hset("bb:route:T1", "number", "")
    assert await scripts.enqueue("T1", "L2", NOW() - 1) == -3  # today's path
    await rr.zadd("bb:q:T1", {"L3": NOW() - 1})
    assert await scripts.reap_lease("N1", "L1", tk, "T1", NOW() - 1) == 0
    assert set(await rr.zrange("bb:q:T1", 0, -1)) == {"L1", "L3"}
    assert await rr.zcard("bb:due") == 0  # no number "" listed
    assert not await rr.exists("bb:numtpl:")
    assert not await rr.sismember("bb:numtpl:N1", "T1")


@pytest.mark.asyncio
async def test_second_give_back_never_frees_a_new_ticket(rr):
    # PoC review race B: give back, the lead is re-queued due now on the same number and
    # re-ticketed, then a caller gives back again. Without ticket ids the second give-back
    # removed the NEW ticket's busy entry and lease, leaving a ticket with no line.
    await seed_number(rr, "N1", 1, {"T1": {}})
    await scripts.enqueue("T1", "L1", NOW() - 1)
    _, old = await _ticket(rr, "N1")
    assert await scripts.return_line("N1", "L1", old, OWNER) == 0
    await scripts.enqueue("T1", "L1", NOW() - 1)  # re-ticketed
    assert await scripts.return_line("N1", "L1", old, OWNER) == -1  # stale: no-op
    assert await rr.smembers("bb:busy:N1") == {"lead:L1"}
    assert await tickets_of(rr, "N1") == ["L1"]


@pytest.mark.asyncio
async def test_return_line_after_dialling_only_when_not_placed(rr):
    # M7: after mark_dialling a call may exist, so a plain give-back is refused (holds
    # the line); only the "provider said not placed" exits may free it.
    await seed_number(rr, "N1", 1, {"T1": {}})
    await scripts.enqueue("T1", "L1", NOW() - 1)
    await scripts.enqueue("T1", "L2", NOW() - 1)
    _, tk = await _ticket(rr, "N1")
    assert await scripts.mark_dialling("N1", "L1", tk, OWNER) is scripts.Mark.DIAL
    assert await scripts.return_line("N1", "L1", tk, OWNER) == -3
    assert await rr.smembers("bb:busy:N1") == {"lead:L1"}
    assert await rr.hexists("bb:inflight:N1", "L1")
    assert await scripts.return_line("N1", "L1", tk, OWNER, not_placed=True) == 1
    assert await rr.smembers("bb:busy:N1") == {"lead:L2"}  # freed and re-matched
    assert not await rr.hexists("bb:inflight:N1", "L1")


@pytest.mark.asyncio
async def test_reaper_loses_to_mark_dialling(rr):
    # race C: the reaper read a lease with no dialling time, then the dial coroutine marked it
    await seed_number(rr, "N1", 1, {"T1": {}})
    await scripts.enqueue("T1", "L1", NOW() - 1)
    _, tk = await _ticket(rr, "N1")
    assert await scripts.mark_dialling("N1", "L1", tk, OWNER) is scripts.Mark.DIAL
    assert (
        await scripts.mark_dialling("N1", "L1", tk, "other") is scripts.Mark.SUPERSEDED
    )
    assert await scripts.reap_lease("N1", "L1", tk, "T1", NOW()) == -1
    assert await rr.smembers("bb:busy:N1") == {"lead:L1"}


@pytest.mark.asyncio
async def test_reaped_ticket_cannot_dial(rr):
    # race D: a slow ticket is reaped and re-issued; the old holder must not dial
    await seed_number(rr, "N1", 1, {"T1": {}})
    await scripts.enqueue("T1", "L1", NOW() - 1)
    _, old = await _ticket(rr, "N1")
    assert (
        await scripts.reap_lease("N1", "L1", old, "T1", NOW() - 1) == 1
    )  # re-queued + re-ticketed
    _, new = await _ticket(rr, "N1")
    assert new != old
    assert (
        await scripts.mark_dialling("N1", "L1", old, OWNER) is scripts.Mark.SUPERSEDED
    )
    assert await scripts.mark_dialling("N1", "L1", new, OWNER) is scripts.Mark.DIAL
    assert (
        await scripts.clear_lease("N1", "L1", old, OWNER) is False
    )  # stale clear: no-op
    assert await rr.hexists("bb:inflight:N1", "L1")
    assert await scripts.clear_lease("N1", "L1", new, OWNER) is True
    assert not await rr.hexists("bb:inflight:N1", "L1")
    assert await rr.smembers("bb:busy:N1") == {"lead:L1"}  # the live call holds it


@pytest.mark.asyncio
async def test_reap_lease_free_only_and_stuck_dial(rr):
    await seed_number(rr, "N1", 1, {"T1": {}})
    await scripts.enqueue("T1", "L1", NOW() - 1)
    _, tk = await _ticket(rr, "N1")
    assert await scripts.reap_lease("N1", "L1", tk, "", NOW()) == 0  # free, no re-queue
    assert await rr.scard("bb:busy:N1") == 0 and await rr.zcard("bb:q:T1") == 0
    await scripts.enqueue("T1", "L2", NOW() - 1)
    _, tk = await _ticket(rr, "N1")
    await scripts.mark_dialling("N1", "L2", tk, OWNER)
    # rule 14: a dial stuck for 10 min may be reaped even though it is dialling
    assert await scripts.reap_lease("N1", "L2", tk, "", NOW(), allow_dialling=True) == 0
    assert await rr.scard("bb:busy:N1") == 0


@pytest.mark.asyncio
async def test_reap_requeue_flags_the_routes_current_number(rr):
    # Fable I2 scenario 2: T1 moved to N2 while L1 held a ticket on N1.
    await seed_number(rr, "N1", 1, {"T1": {}})
    await scripts.enqueue("T1", "L1", NOW() - 1)
    _, tk = await _ticket(rr, "N1")
    await seed_number(rr, "N2", 1, {"T1": {}})
    await rr.srem("bb:numtpl:N1", "T1")
    await rr.srem("bb:numtpl:N2", "T1")  # and the SADD on N2 was lost
    assert await scripts.reap_lease("N1", "L1", tk, "T1", NOW() - 1) == 0
    assert await rr.zrange("bb:q:T1", 0, -1) == ["L1"]
    assert await rr.zscore("bb:due", "N2") is not None  # due when the lead is
    assert await rr.sismember("bb:numtpl:N2", "T1")
    assert await scripts.match("N2") == 1


@pytest.mark.asyncio
async def test_release_stale_removes_only_holders_without_a_lease(rr):
    await seed_number(rr, "N1", 2, {"T1": {}})
    await scripts.enqueue("T1", "L1", NOW() - 1)  # ticket: has a lease
    await rr.sadd("bb:busy:N1", "lead:L9")  # stale holder, no lease
    await scripts.enqueue("T1", "L2", NOW() - 1)  # waits: number is full
    assert await scripts.release_stale("N1", "L1") == 0
    assert await scripts.release_stale("N1", "L9") == 1
    assert await rr.smembers("bb:busy:N1") == {"lead:L1", "lead:L2"}  # re-matched


@pytest.mark.asyncio
async def test_match_caps_tickets_per_call(rr):
    await seed_number(rr, "N1", 1000, {"T1": {}})
    await rr.zadd("bb:q:T1", {f"L{i}": NOW() - 1 for i in range(300)})
    assert await scripts.match("N1") == 100


@pytest.mark.asyncio
async def test_admit_inbound_respects_max_and_is_idempotent(rr):
    await seed_number(rr, "N1", 1, {"T1": {}})
    assert await scripts.admit_inbound("N1", "C1") is True
    assert await scripts.admit_inbound("N1", "C1") is True  # retried webhook
    assert await scripts.admit_inbound("N1", "C2") is False
    assert await rr.smembers("bb:busy:N1") == {"call:C1"}


@pytest.mark.asyncio
async def test_admit_inbound_without_number_facts_refuses(rr):
    assert await scripts.admit_inbound("N9", "C1") is False
    assert await rr.scard("bb:busy:N9") == 0


@pytest.mark.asyncio
async def test_release_of_inbound_call_hands_line_to_waiting_lead(rr):
    await seed_number(rr, "N1", 1, {"T1": {}})
    assert await scripts.admit_inbound("N1", "C1") is True
    assert await scripts.enqueue("T1", "L1", NOW() - 1) == 0  # full: waits
    assert await scripts.release("N1", "call:C1") == [1, 1]
    assert await rr.smembers("bb:busy:N1") == {"lead:L1"}


def _clock(sec: int) -> dt.time:
    return dt.time(sec // 3600, sec % 3600 // 60, sec % 60)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start,end,sec",
    [
        (32400, 75600, 32400),  # 09:00–21:00: 09:00:00 inclusive
        (32400, 75600, 32399),  # 08:59:59 closed
        (32400, 75600, 75600),  # 21:00:00 inclusive
        (32400, 75600, 75601),  # 21:00:01 closed
        (79200, 21600, 79200),  # 22:00–06:00 wraps; 22:00 open
        (79200, 21600, 0),  # midnight open
        (79200, 21600, 3600),  # 01:00 open
        (79200, 21600, 21600),  # 06:00:00 inclusive
        (79200, 21600, 21601),  # 06:00:01 closed
        (79200, 21600, 43200),  # 12:00 closed
        (36000, 36000, 36000),  # start == end: that one second only
        (36000, 36000, 36001),
        (0, 86399, 86399),  # whole day
    ],
)
async def test_hours_parity_with_python_rule(rr, start, end, sec):
    python = hours_open(_clock(start), _clock(end), _clock(sec))
    assert await scripts.hours_open_for_test(start, end, sec) is python


class _EmptyEval:
    async def execute_command(self, *args):
        return None  # an empty reply


class _RaisingEval:
    async def execute_command(self, *args):
        raise ConnectionError("redis down")


class _MalformedEval:
    async def execute_command(self, *args):
        return "?"  # a reply none of the scripts gives


@pytest.mark.asyncio
@pytest.mark.parametrize("client", [_EmptyEval(), _RaisingEval(), _MalformedEval()])
async def test_wrappers_report_failure_instead_of_raising(monkeypatch, client):
    monkeypatch.setattr(redis_client, "_client", client)
    assert await scripts.enqueue("T1", "L1", 0) is None
    assert await scripts.match("N1") is None
    assert await scripts.claim("N1", "L1", 1, OWNER) is False
    assert await scripts.return_line("N1", "L1", 1, OWNER) is None
    assert await scripts.release("N1", "lead:L1") is None
    assert await scripts.mark_dialling("N1", "L1", 1, OWNER) is scripts.Mark.REFUSED
    assert await scripts.clear_lease("N1", "L1", 1, OWNER) is False
    assert await scripts.admit_inbound("N1", "C1") is None
    assert await scripts.reap_lease("N1", "L1", 1, "T1", 0) is None
    assert await scripts.release_stale("N1", "L1") is None


@pytest.mark.asyncio
async def test_hours_helper_fails_loudly_on_redis_error(monkeypatch):
    monkeypatch.setattr(redis_client, "_client", _EmptyEval())
    with pytest.raises(RuntimeError):
        await scripts.hours_open_for_test(0, 86399, 0)


@pytest.mark.asyncio
async def test_match_unlists_a_number_todays_dialler_owns(rr):
    """Fable M1: a legacy number in bb:due (a route moved there with leads in its room) is
    removed by its first match, or the sweep pays an EVAL for it every second forever.
    v2_pending and draining keep their entry: their rooms are matched once the mode is v2
    again."""
    for n, mode in (
        ("NA", None),
        ("NL", "legacy"),
        ("NP", "v2_pending"),
        ("ND", "draining"),
    ):
        await seed_number(rr, n, 2, {f"T{n}": {}}, mode=mode)
        await rr.zadd(f"bb:q:T{n}", {f"L{n}": NOW() - 1})
        await rr.zadd("bb:due", {n: NOW() - 1})
        assert await scripts.match(n) == 0
    assert set(await rr.zrange("bb:due", 0, -1)) == {"NP", "ND"}


@pytest.mark.asyncio
async def test_enqueue_only_if_absent_leaves_a_queued_lead_untouched(rr):
    # The backlog reconciler re-reads a big pile: a lead already in its room keeps its
    # score (a stale page can't move a deferred lead earlier) and no match runs.
    await seed_number(rr, "N1", 0, {"T1": {}})  # full: the lead waits in its room
    later = NOW() + 600_000
    assert await scripts.enqueue("T1", "L1", later) == 0
    assert await scripts.enqueue("T1", "L1", NOW() - 1, only_if_absent=True) == 0
    assert await rr.zscore("bb:q:T1", "L1") == later
    # a lead missing from its room is added as before
    assert await scripts.enqueue("T1", "L2", NOW() - 1, only_if_absent=True) == 0
    assert await rr.zscore("bb:q:T1", "L2") is not None


@pytest.mark.asyncio
async def test_match_on_a_full_number_returns_before_scanning_templates(rr):
    # A full number returns at once and leaves bb:due: every script that frees a line
    # runs match, which issues as usual.
    await seed_number(rr, "N1", 1, {"T1": {}})
    assert await scripts.enqueue("T1", "L1", NOW() - 2) == 1
    assert await scripts.enqueue("T1", "L2", NOW() - 1) == 0  # full
    assert await scripts.match("N1") == 0
    assert await rr.zscore("bb:due", "N1") is None
    assert await rr.zrange("bb:q:T1", 0, -1) == ["L2"]
    lead, tk = await _ticket(rr, "N1")
    assert (
        await scripts.return_line("N1", lead, tk, OWNER, not_placed=True) == 1
    )  # L2 issued
    assert await tickets_of(rr, "N1") == ["L2"]


@pytest.mark.asyncio
async def test_a_full_shared_number_skips_its_template_pass(rr):
    # 300 templates on one full number: the 1 s sweep's match must not read every route
    # (the measured 12-21 % of a Redis core). Commands run inside Lua are counted too.
    await seed_number(rr, "N1", 1, {f"T{i}": {} for i in range(300)})
    assert await scripts.enqueue("T0", "L0", NOW() - 3) == 1  # takes the only line
    for i in range(1, 300):
        await rr.zadd(f"bb:q:T{i}", {f"L{i}": NOW() - 2})
    await rr.zadd("bb:due", {"N1": NOW() - 2})
    await rr.config_resetstat()
    assert await scripts.match("N1") == 0
    stats = await rr.info("commandstats")
    assert "cmdstat_hmget" not in stats and "cmdstat_zrange" not in stats
    assert await rr.zscore("bb:due", "N1") is None  # full: a freed line wakes it


@pytest.mark.asyncio
async def test_a_legacy_number_still_leaves_bb_due(rr):
    await seed_number(rr, "N1", 0, {"T1": {}}, mode="legacy")
    await rr.zadd("bb:due", {"N1": NOW() - 1})
    assert await scripts.match("N1") == 0
    assert await rr.zscore("bb:due", "N1") is None


@pytest.mark.asyncio
async def test_a_freed_line_on_a_full_number_goes_to_the_oldest_due_lead(rr):
    await seed_number(rr, "N1", 1, {"T1": {}, "T2": {}})
    now = (
        NOW()
    )  # one clock read: separate reads a millisecond apart could swap the order
    assert await scripts.enqueue("T1", "L1", now - 3) == 1
    assert await scripts.enqueue("T2", "NEWER", now - 1) == 0  # full
    assert await scripts.enqueue("T1", "OLDER", now - 2) == 0  # full
    assert await scripts.match("N1") == 0
    assert (await _ticket(rr, "N1"))[0] == "L1"  # a dial coroutine took L1's ticket
    assert await scripts.release("N1", "lead:L1") == [1, 1]  # removed, issued
    assert await tickets_of(rr, "N1") == ["OLDER"]


# -- review #1287 finding 6: match reads each room once per run; rooms move in chunks ------


def _old_match_order(rooms, boost, free, cap):
    """The rule before finding 6, re-reading every room for every ticket: the due head with
    the lowest (score - tier boost) wins; a lead already ticketed only loses its copy.
    """
    rooms = {t: sorted(items, key=lambda x: x[1]) for t, items in rooms.items()}
    got, issued, now = [], 0, NOW()
    while free > 0 and issued < cap:
        best: Optional[Tuple[int, str]] = None
        for t, items in rooms.items():
            if items and items[0][1] <= now:
                rank = items[0][1] - boost[t]
                if best is None or rank < best[0]:
                    best = (rank, t)
        if best is None:
            break
        lead, _ = rooms[best[1]].pop(0)
        if lead in got:
            continue
        got.append(lead)
        free, issued = free - 1, issued + 1
    return got


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
async def test_match_picks_exactly_the_leads_the_old_rule_picked(rr, seed):
    rnd = random.Random(seed)
    boosts = {"normal": 0, "medium": 300_000, "high": 600_000}
    tiers = {f"T{i}": rnd.choice(list(boosts)) for i in range(12)}
    await seed_number(rr, "N1", 40, {t: {"tier": tier} for t, tier in tiers.items()})
    now = NOW()
    rooms: dict = {t: [] for t in tiers}
    # scores 7 ms apart: no two due heads of different rooms tie on (score - boost)
    for j, u in enumerate(rnd.sample(range(1, 50_000), 160)):
        due = rnd.random() < 0.85
        rooms[f"T{j % 12}"].append(
            (f"L{j}", now - 7 * u if due else now + 60_000 + 7 * u)
        )
    u1, u2 = rnd.sample(range(50_001, 60_000), 2)
    rooms["T0"].append(("DUP", now - 7 * u1))  # one lead queued under two templates
    rooms["T1"].append(("DUP", now - 7 * u2))
    for t, items in rooms.items():
        await rr.zadd(f"bb:q:{t}", dict(items))
    want = _old_match_order(rooms, {t: boosts[x] for t, x in tiers.items()}, 40, 100)
    assert await scripts.match("N1") == len(want) == 40
    assert await tickets_of(rr, "N1") == want


@pytest.mark.asyncio
async def test_a_burst_on_a_number_shared_by_300_templates_reads_each_room_about_once(
    rr,
):
    await seed_number(rr, "N1", 100, {f"T{i}": {} for i in range(300)})
    now = NOW()
    pipe = rr.pipeline(transaction=False)
    for i in range(300):
        pipe.zadd(f"bb:q:T{i}", {f"L{i}a": now - 1000 - i, f"L{i}b": now - 500 - i})
    await pipe.execute()
    await rr.config_resetstat()
    assert await scripts.match("N1") == 100
    reads = (await rr.info("commandstats"))["cmdstat_zrange"]["calls"]
    assert reads <= 300 + 100  # re-reading every room per ticket was 300 x 100 = 30,000


@pytest.mark.asyncio
async def test_a_big_room_moves_to_todays_schedule_in_chunks(rr, monkeypatch):
    now = NOW()
    await rr.zadd("bb:q:T1", {f"L{i}": now + i for i in range(2500)})
    chunks = []
    real = scripts._run

    async def counting(script, args, parse):
        if script is scripts.MOVE_ROOM_LUA:
            chunks.append(args)
        return await real(script, args, parse)

    monkeypatch.setattr(scripts, "_run", counting)
    assert await scripts.move_room_to_schedule("T1") == 2500
    assert len(chunks) == 3  # 1,000 + 1,000 + 500: no single 2,500-lead script
    assert not await rr.exists("bb:q:T1")
    assert await rr.zcard(scripts.SCHEDULE_ZSET) == 2500
    assert await rr.zscore(scripts.SCHEDULE_ZSET, "L7") == now + 7


@pytest.mark.asyncio
async def test_a_room_move_that_fails_half_way_keeps_the_rest_in_the_room(
    rr, monkeypatch
):
    await rr.zadd("bb:q:T1", {f"L{i}": i for i in range(1500)})
    real, seen = scripts._run, []

    async def second_call_fails(script, args, parse):
        if script is scripts.MOVE_ROOM_LUA:
            seen.append(1)
            if len(seen) == 2:
                return None
        return await real(script, args, parse)

    monkeypatch.setattr(scripts, "_run", second_call_fails)
    assert await scripts.move_room_to_schedule("T1") is None
    assert await rr.zcard("bb:q:T1") == 500  # the next try moves these
    assert await rr.zcard(scripts.SCHEDULE_ZSET) == 1000


@pytest.mark.asyncio
async def test_match_many_runs_every_number_and_isolates_a_failing_one(rr):
    await seed_number(rr, "N1", 1, {"T1": {}})
    await seed_number(rr, "N2", 1, {"T2": {}})
    await rr.set("bb:busy:N1", "not-a-set")  # SCARD fails inside N1's script
    await rr.zadd("bb:q:T2", {"L2": NOW() - 1})
    assert await scripts.match_many(["N1", "N2"]) == {"N1": None, "N2": 1}
    assert await scripts.match_many([]) == {}


async def test_a_room_of_leads_that_already_hold_lines_is_skipped_in_bounded_steps(rr):
    # each such copy is only removed (a second ticket would dial it twice); one run
    # removes at most cap + open rooms of them, and the number stays due now so the next
    # tick goes on, instead of one script walking the whole room
    await seed_number(rr, "N1", 10, {"T1": {}})
    now = int(time.time() * 1000)
    copies = {f"L{i}": now - 1 for i in range(300)}
    await rr.zadd("bb:q:T1", copies)
    await rr.hset("bb:inflight:N1", mapping={lead: "{}" for lead in copies})
    assert await scripts.match("N1") == 0
    assert await rr.zcard("bb:q:T1") == 300 - (scripts.BB_V2_MATCH_CAP + 1)
    assert await rr.zscore("bb:due", "N1") <= int(time.time() * 1000)

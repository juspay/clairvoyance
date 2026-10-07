"""One acceptor per pod, one coroutine per ticket (spec 2026-10-05 §4.1-4.3, design card
§4). Real Redis; the dial (or the worker's dispatch) is faked and coordinated with events.
"""

import asyncio
import json
import socket
import time
from types import SimpleNamespace as NS
from typing import Any, List
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch import worker as W
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    acceptor as A,
    reconcile as RC,
    redis_client,
    routes,
    scripts,
)
from tests.breeze_buddy.dispatch.v2.conftest import seed_number, use_redis

pytestmark = pytest.mark.asyncio


def NOW() -> int:
    return int(time.time() * 1000)


@pytest.fixture
def live(monkeypatch) -> AsyncMock:
    """v2 in use on this pod and the kill switch off; the returned mock is its read."""
    enabled = AsyncMock(return_value=True)
    monkeypatch.setattr(A, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(A.dyn_cfg, "BB_DISPATCH_ENABLED", enabled)
    for pause in (
        "BB_V2_ACCEPT_DISABLED_SLEEP_S",
        "BB_V2_ACCEPT_ERROR_BACKOFF_S",
        "BB_V2_ACCEPT_FULL_SLEEP_S",
    ):
        monkeypatch.setattr(A, pause, 0.01)
    monkeypatch.setattr(A, "BB_V2_TICKET_BLPOP_TIMEOUT_S", 0.1)
    return enabled


@pytest.fixture
def requeued(monkeypatch) -> AsyncMock:
    schedule = AsyncMock(return_value=True)
    monkeypatch.setattr(A, "schedule_lead", schedule)
    return schedule


async def _fill(rr, n: str, leads: int, lines: int) -> None:
    await seed_number(rr, n, lines, {f"T-{n}": {}})
    for i in range(leads):
        assert await scripts.enqueue(f"T-{n}", f"{n}-L{i}", NOW() - 1) is not None


async def _first(rr) -> scripts.Ticket:
    t = scripts.parse_ticket(await rr.lpop("bb:tickets"))
    assert t is not None
    return t


def _worker(dispatch) -> Any:
    return NS(_dispatch=dispatch)


class Gate:
    """A fake dial that blocks every ticket until ``release``; ``reached`` fires once
    ``expect`` tickets are inside it at the same time."""

    def __init__(self, expect: int) -> None:
        self.expect = expect
        self.inside: List[str] = []
        self.workers: set = set()
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, t, session, worker, stopping) -> bool:
        self.inside.append(t.lead_id)
        self.workers.add(id(worker))
        if len(self.inside) == self.expect:
            self.reached.set()
        await self.release.wait()
        return True


async def test_every_ticket_gets_its_own_coroutine_at_once(rr, live):
    await _fill(rr, "N1", 300, 300)
    dial = Gate(expect=300)
    acc = A.Acceptor(dial=dial)
    acc.start()
    await asyncio.wait_for(dial.reached.wait(), 5)  # no pool: all 300 at once
    assert acc.in_flight == 300
    assert len(dial.workers) == 300  # each coroutine its own Worker (its own state)
    dial.release.set()
    await acc.stop(grace_s=5)
    assert acc.in_flight == 0


async def test_a_slow_number_does_not_delay_another(rr, live):
    await _fill(rr, "SLOW", 200, 200)
    hang, fast = asyncio.Event(), Gate(expect=5)
    fast.release.set()

    async def dial(t, session, worker, stopping):
        if t.number_id == "SLOW":
            await hang.wait()  # its greeting TTS hangs
            return True
        return await fast(t, session, worker, stopping)

    acc = A.Acceptor(dial=dial)
    acc.start()
    await _fill(rr, "FAST", 5, 5)
    await asyncio.wait_for(fast.reached.wait(), 5)
    assert acc.in_flight == 200  # every SLOW dial still hangs
    hang.set()
    await acc.stop(grace_s=5)


async def test_a_ticket_delivered_twice_dials_once(rr):
    await _fill(rr, "N1", 1, 1)
    t = await _first(rr)
    dispatched = []

    async def dispatch(lead_id, session, held):
        dispatched.append(held.owner)
        return True

    results = await asyncio.gather(
        A.dial_ticket(t, None, _worker(dispatch), asyncio.Event()),
        A.dial_ticket(t, None, _worker(dispatch), asyncio.Event()),
    )
    assert sorted(results) == [False, True]
    assert len(dispatched) == 1


async def test_a_batch_popped_while_the_kill_switch_is_on_goes_back(rr, live):
    await _fill(rr, "N1", 50, 50)
    waiting = await rr.lrange("bb:tickets", 0, -1)
    live.return_value = False
    acc = A.Acceptor(dial=AsyncMock(side_effect=AssertionError("dialled")))
    await acc._round()
    assert await rr.lrange("bb:tickets", 0, -1) == waiting  # in order, untouched
    assert live.await_count == 1


async def test_the_kill_switch_is_read_once_per_batch(rr, live):
    await _fill(rr, "N1", 50, 50)
    dial = Gate(expect=50)
    dial.release.set()
    acc = A.Acceptor(dial=dial)
    await acc._round()
    await asyncio.wait_for(dial.reached.wait(), 5)
    assert live.await_count == 1  # not once per dial (Phase 3 b)
    await acc.stop(grace_s=5)


async def test_a_ticket_claimed_while_the_pod_stops_goes_back_to_its_room(rr, requeued):
    await _fill(rr, "N1", 1, 1)
    t = await _first(rr)
    stopping = asyncio.Event()
    stopping.set()
    w = _worker(AsyncMock(side_effect=AssertionError("dispatched")))
    assert await A.dial_ticket(t, None, w, stopping) is False
    assert await rr.scard("bb:busy:N1") == 0
    assert requeued.await_args.args[0] == t.lead_id
    assert requeued.await_args.kwargs["template_id"] == "T-N1"


async def test_a_cancelled_dispatch_gives_back_and_requeues(rr, requeued):
    await _fill(rr, "N1", 1, 1)
    t = await _first(rr)
    entered = asyncio.Event()

    async def dispatch(lead_id, session, held):
        entered.set()
        await asyncio.Event().wait()  # its checks hang

    task = asyncio.create_task(
        A.dial_ticket(t, None, _worker(dispatch), asyncio.Event())
    )
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await rr.scard("bb:busy:N1") == 0
    requeued.assert_awaited_once()


async def test_a_cancel_after_mark_keeps_the_line_and_does_not_requeue(rr, requeued):
    await _fill(rr, "N1", 1, 1)
    t = await _first(rr)
    entered = asyncio.Event()

    async def dispatch(lead_id, session, held):
        mark = await scripts.mark_dialling(held.number_id, lead_id, held.tk, held.owner)
        assert mark is scripts.Mark.DIAL
        entered.set()
        await asyncio.Event().wait()  # the dial is on the wire

    task = asyncio.create_task(
        A.dial_ticket(t, None, _worker(dispatch), asyncio.Event())
    )
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await rr.sismember("bb:busy:N1", f"lead:{t.lead_id}")
    requeued.assert_not_awaited()


async def test_stop_cancels_uncommitted_dispatches_and_waits_for_committed(rr, live):
    await _fill(rr, "N1", 4, 4)
    outcome: dict = {}
    inside, finish = Gate(expect=4), asyncio.Event()

    async def dial(t, session, worker, stopping):
        committed = t.lead_id in ("N1-L0", "N1-L1")
        worker._phase = W._COMMITTED if committed else W._PRE_DIAL
        try:
            inside.inside.append(t.lead_id)
            if len(inside.inside) == 4:
                inside.reached.set()
            await (finish.wait() if committed else asyncio.Event().wait())
            outcome[t.lead_id] = "done"
        except asyncio.CancelledError:
            outcome[t.lead_id] = "cancelled"
            raise
        return True

    acc = A.Acceptor(dial=dial)
    acc.start()
    await asyncio.wait_for(inside.reached.wait(), 5)
    stopping = asyncio.create_task(acc.stop(grace_s=5))
    finish.set()  # the committed dials end; the others hang until cancelled
    await asyncio.wait_for(stopping, 5)
    assert outcome == {
        "N1-L0": "done",
        "N1-L1": "done",
        "N1-L2": "cancelled",
        "N1-L3": "cancelled",
    }
    assert await rr.llen("bb:tickets") == 0


async def test_entries_popped_while_stopping_go_back_in_order(rr, live):
    await _fill(rr, "N1", 5, 5)
    waiting = await rr.lrange("bb:tickets", 0, -1)
    acc = A.Acceptor(dial=AsyncMock(side_effect=AssertionError("dialled")))
    await acc.stop(grace_s=0)
    await acc._round()
    assert await rr.lrange("bb:tickets", 0, -1) == waiting


async def test_a_pod_that_never_used_v2_never_blpops(monkeypatch):
    monkeypatch.setattr(A, "v2_seen", AsyncMock(return_value=False))
    monkeypatch.setattr(A, "REFRESH_S", 0.01)
    client = NS(blpop=AsyncMock(side_effect=AssertionError("BLPOP while v2 unused")))
    monkeypatch.setattr(redis_client, "_client", client)
    await A.Acceptor(dial=AsyncMock())._round()
    client.blpop.assert_not_awaited()


async def test_the_in_flight_guard_stops_popping_when_the_pod_is_full(
    rr, live, monkeypatch
):
    monkeypatch.setattr(A, "BB_V2_MAX_INFLIGHT_PER_POD", 10)
    await _fill(rr, "N1", 50, 50)
    dial = Gate(expect=10)
    acc = A.Acceptor(dial=dial)
    acc.start()
    await asyncio.wait_for(dial.reached.wait(), 5)
    await acc._round()  # full: pops nothing
    assert acc.in_flight == 10
    assert await rr.llen("bb:tickets") == 40  # left for the other pods
    dial.release.set()
    await acc.stop(grace_s=5)


async def test_a_redis_error_does_not_end_the_acceptor(live, monkeypatch):
    errors: List[int] = []
    twice = asyncio.Event()

    async def broken():
        errors.append(1)
        if len(errors) == 2:
            twice.set()
        raise RedisConnectionError("down")

    monkeypatch.setattr(A, "v2_redis", broken)
    acc = A.Acceptor(dial=AsyncMock())
    acc.start()
    await asyncio.wait_for(twice.wait(), 5)
    assert acc.running
    await acc.stop(grace_s=1)


async def test_a_lost_ticket_issued_with_the_head_in_one_run_is_delivered_again(
    rr, monkeypatch
):
    # One match run issues every ticket with one issued_ms, so "issued before
    # the head" can't tell them apart by time alone; their per-number ticket ids can
    use_redis(monkeypatch, rr, RC, routes)
    await rr.sadd("bb:v2:active", "N1")
    await seed_number(rr, "N1", 3, {"T-N1": {}})
    await rr.zadd("bb:q:T-N1", {f"N1-L{i}": NOW() - 1 for i in range(3)})
    assert await scripts.match("N1") == 3  # one run: three tickets, one issued_ms
    lost = scripts.parse_ticket(await rr.lpop("bb:tickets"))  # popped, reply lost
    assert lost is not None
    for lead in ("N1-L0", "N1-L1", "N1-L2"):
        # 31 s since its last delivery; its issue time stays the head's, as in the list
        lease = json.loads(await rr.hget("bb:inflight:N1", lead))
        lease["repushed_ms"] = lease["issued_ms"] - 31_000
        await rr.hset("bb:inflight:N1", lead, json.dumps(lease))
    head = await rr.lindex("bb:tickets", 0)
    monkeypatch.setattr(RC, "get_lead_dispatch_states", AsyncMock(return_value={}))
    assert await RC.reap_leases() == 1  # the lost one; the two still waiting are not
    back = await rr.lrange("bb:tickets", 0, -1)
    assert len(back) == 3 and back[1] == head
    first = scripts.parse_ticket(back[0])
    assert first is not None and first.lead_id == lost.lead_id


async def test_tickets_whose_pop_reply_was_lost_are_delivered_again(
    rr, live, monkeypatch
):
    use_redis(monkeypatch, rr, RC, routes)
    await rr.sadd("bb:v2:active", "N1")
    await seed_number(rr, "N1", 3, {"T-N1": {}})
    await rr.zadd("bb:q:T-N1", {f"N1-L{i}": NOW() - 1 for i in range(3)})
    assert await scripts.match("N1") == 3  # one run: three tickets, one issued_ms
    issued = await rr.lrange("bb:tickets", 0, -1)
    real_lpop = rr.lpop

    async def lost_lpop(*a, **k):
        await real_lpop(*a, **k)  # Redis popped them; the reply never arrived
        raise RedisConnectionError("reply lost")

    monkeypatch.setattr(rr, "lpop", lost_lpop)
    acc = A.Acceptor(dial=AsyncMock(side_effect=AssertionError("dialled")))
    with pytest.raises(RedisConnectionError):
        await acc._round()
    monkeypatch.setattr(rr, "lpop", real_lpop)
    assert await rr.llen("bb:tickets") == 0  # out of the list, claimed by nobody
    for lead in ("N1-L0", "N1-L1", "N1-L2"):
        lease = json.loads(await rr.hget("bb:inflight:N1", lead))
        lease["issued_ms"] -= 31_000  # all three still one issued_ms
        await rr.hset("bb:inflight:N1", lead, json.dumps(lease))
    monkeypatch.setattr(RC, "get_lead_dispatch_states", AsyncMock(return_value={}))
    assert await RC.reap_leases() == 3  # all in one run, not one per run
    back = await rr.lrange("bb:tickets", 0, -1)
    assert sorted(x.rsplit("|", 1)[0] for x in back) == sorted(
        x.rsplit("|", 1)[0] for x in issued
    )


async def test_many_acceptors_claim_each_ticket_once(rr, live):
    await _fill(rr, "N1", 200, 200)
    claimed: List[str] = []
    done = asyncio.Event()

    async def dial(t, session, worker, stopping):
        if await scripts.claim(t.number_id, t.lead_id, t.tk, f"o-{id(worker)}"):
            claimed.append(t.lead_id)
            if len(claimed) == 200:
                done.set()
        return False

    accs = [A.Acceptor(dial=dial) for _ in range(4)]
    for a in accs:
        a.start()
    await asyncio.wait_for(done.wait(), 5)
    for a in accs:
        await a.stop(grace_s=1)
    assert len(set(claimed)) == len(claimed) == 200


async def test_start_is_idempotent(monkeypatch):
    monkeypatch.setattr(A, "v2_seen", AsyncMock(return_value=False))
    monkeypatch.setattr(A, "REFRESH_S", 0.01)
    monkeypatch.setattr(A, "_acceptor", None)
    a1 = await A.start_acceptor()
    a2 = await A.start_acceptor()
    assert a1 is a2 and a1.running
    await A.stop_acceptor(grace_s=1)
    assert not a1.running


async def test_an_unreadable_entry_is_dropped_and_the_rest_dial(rr, live):
    await _fill(rr, "N1", 2, 2)
    await rr.lpush("bb:tickets", "garbage")
    dial = Gate(expect=2)
    dial.release.set()
    acc = A.Acceptor(dial=dial)
    await acc._round()
    await asyncio.wait_for(dial.reached.wait(), 5)
    assert sorted(dial.inside) == ["N1-L0", "N1-L1"]
    await acc.stop(grace_s=1)


async def test_a_dial_outliving_the_grace_keeps_the_session_until_it_ends(rr, live):
    # A dial still running after the shutdown grace may still use the pod's
    # session; it is closed once the last such dial ends, not left open for good
    await _fill(rr, "N1", 1, 1)
    dial = Gate(expect=1)  # never cancel-safe: past its commit point
    acc = A.Acceptor(dial=dial)
    acc.start()
    await asyncio.wait_for(dial.reached.wait(), 5)
    session = acc._session
    assert session is not None
    await acc.stop(grace_s=0.05)
    assert not session.closed  # the dial may still need it
    dial.release.set()
    for _ in range(50):
        if session.closed:
            break
        await asyncio.sleep(0.01)
    assert session.closed


async def test_the_pods_http_session_never_queues_a_dispatch(rr, live):
    """Every coroutine's merchant pre-check goes through the pod's one HTTP session; a
    connection limit there would be a hidden pool again (aiohttp's default is 100)."""
    from aiohttp import web

    n = 120  # above aiohttp's default limit of 100, within the OS listen backlog
    arrived, all_in = [], asyncio.Event()

    async def handler(request):
        arrived.append(1)
        if len(arrived) == n:
            all_in.set()
        await all_in.wait()  # every request is in flight at once, or none returns
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    await web.SockSite(runner, sock, backlog=n).start()
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
    done = Gate(expect=n)
    done.release.set()

    async def dial(t, session, worker, stopping):
        async with session.get(url) as reply:
            await reply.text()
        return await done(t, session, worker, stopping)

    await _fill(rr, "N1", n, n)
    acc = A.Acceptor(dial=dial)
    acc.start()
    try:
        await asyncio.wait_for(all_in.wait(), 5)
        await asyncio.wait_for(done.reached.wait(), 5)
    finally:
        all_in.set()  # a failed run lets its queued requests finish too
        await acc.stop(grace_s=5)
        await runner.cleanup()

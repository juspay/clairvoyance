"""The grant worker and the call-queue hooks (real Redis, a fake CRM contract).

The worker takes a ``bb:grants`` entry (a line is reserved, the lead row does not exist
yet), asks the CRM to make the lead, and then publishes the ticket or gives the line back.
"""

from __future__ import annotations

import importlib
import inspect
import json
import time
from datetime import datetime, timezone

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    grants,
    intents,
    routes,
    scripts,
)
from app.crm.outreach import contracts, waiting_calls as call_queue
from app.crm.outreach.contracts import Refusal
from app.crm.outreach.waiting_calls import WaitingCall
from tests.breeze_buddy.dispatch.v2.conftest import seed_number, use_redis

pytestmark = pytest.mark.asyncio

BAND = 10**13


def NOW() -> int:
    return int(time.time() * 1000)


async def granted(rr, lines=1, members=("L1",)):
    await seed_number(rr, "N1", lines, {"T1": {}})
    await rr.hset("bb:num:N1", "intents", "1")
    for m in members:
        await scripts.enqueue("T1", m, NOW() - 5, run_id=f"run-{m}")


def worker(answers):
    """A worker whose CRM contract answers from ``answers`` (lead id → reply)."""
    calls = []

    async def materialize(run_id, lead_id, number_id):
        calls.append((run_id, lead_id, number_id))
        reply = answers[lead_id]
        if isinstance(reply, Exception):
            raise reply
        return reply

    return grants.GrantWorker(materialize), calls


@pytest.fixture(autouse=True)
def _v2_in_use(monkeypatch):
    async def seen():
        return True

    monkeypatch.setattr(grants, "v2_seen", seen)
    monkeypatch.setattr(intents, "v2_seen", seen)


async def test_a_made_lead_gets_its_ticket(rr):
    await granted(rr)
    w, calls = worker({"L1": "L1"})
    await w._round()
    assert calls == [("run-L1", "L1", "N1")]
    (ticket,) = await rr.lrange("bb:tickets", 0, -1)
    assert ticket.startswith("N1|L1|")
    assert "g" not in json.loads(await rr.hget("bb:inflight:N1", "L1"))
    assert await rr.llen("bb:grants") == 0


async def test_error_leaves_the_lease_alone(rr):
    await granted(rr)
    w, _ = worker({"L1": Refusal.ERROR})
    await w._round()
    assert json.loads(await rr.hget("bb:inflight:N1", "L1"))["g"] == 1
    assert await rr.smembers("bb:busy:N1") == {"lead:L1"}
    assert await rr.hget("bb:qi:T1", "L1") == "run-L1"
    assert await rr.llen("bb:tickets") == 0


async def test_another_refusal_frees_the_line_and_withdraws(rr):
    await granted(rr)
    w, _ = worker({"L1": Refusal.MOVED})
    await w._round()
    for key in ("bb:busy:N1", "bb:inflight:N1", "bb:qi:T1", "bb:q:T1", "bb:tickets"):
        assert not await rr.exists(key)


async def test_a_refusal_cut_short_leaves_nothing_that_blocks_the_hand_back(
    rr, monkeypatch
):
    """The worker dies between the two steps of a refusal. The call's ``bb:qi`` field
    must not be what is left: it is in no room, so no job would remove it, and the
    number could never be handed back (switch: "calls with no lead row still wait").
    What is left is the lease, which the reaper's regrant frees."""
    await granted(rr)
    steps = {"reap_lease": scripts.reap_lease, "withdraw": scripts.withdraw}
    ran = []

    def step(name):
        async def run(*args, **kw):
            ran.append(name)
            if len(ran) > 1:
                raise ConnectionError("the pod died")
            return await steps[name](*args, **kw)

        return run

    for name in steps:
        monkeypatch.setattr(scripts, name, step(name))
    w, _ = worker({"L1": Refusal.MOVED})
    await w._round()
    assert len(ran) == 2

    assert not await rr.exists("bb:qi:T1", "bb:q:T1")  # the hand-back's condition
    tk = json.loads(await rr.hget("bb:inflight:N1", "L1"))["tk"]
    assert await scripts.regrant("N1", "L1", tk, 0, 0) == 2  # BB_V2_GRANT_MAX_S passed
    assert not await rr.exists("bb:busy:N1", "bb:inflight:N1", "bb:q:T1")


async def test_a_refusal_whose_withdraw_failed_is_sent_again(rr, monkeypatch):
    """A Redis error is answered None, not raised. The line is not freed over a member
    that was not withdrawn (its ``bb:qi`` field would be left behind): the lease stays,
    the reaper sends the entry again, and the refusal is finished then."""
    await granted(rr)
    real = scripts.withdraw

    async def failed(*args):
        return None

    monkeypatch.setattr(scripts, "withdraw", failed)
    w, _ = worker({"L1": Refusal.MOVED})
    await w._round()
    lease = json.loads(await rr.hget("bb:inflight:N1", "L1"))
    assert lease["g"] == 1

    monkeypatch.setattr(scripts, "withdraw", real)
    assert await scripts.regrant("N1", "L1", lease["tk"], 0, 600_000) == 1
    await w._round()
    for key in ("bb:busy:N1", "bb:inflight:N1", "bb:qi:T1", "bb:q:T1", "bb:grants"):
        assert not await rr.exists(key)


async def test_a_lead_whose_line_was_taken_back_is_queued_as_a_lead(rr, monkeypatch):
    await granted(rr)
    queued = []

    async def schedule_lead(lead_id, when, **kw):
        queued.append((lead_id, kw))
        return True

    monkeypatch.setattr(grants, "schedule_lead", schedule_lead)

    async def materialize(run_id, lead_id, number_id):
        tk = json.loads(await rr.hget("bb:inflight:N1", "L1"))["tk"]
        await scripts.reap_lease("N1", "L1", tk, "", 0)  # the reaper got there first
        return "L1"

    await grants.GrantWorker(materialize)._round()
    assert await rr.llen("bb:tickets") == 0
    assert queued == [("L1", {"template_id": "T1", "only_if_absent": True})]
    assert not await rr.hexists("bb:qi:T1", "L1")  # it has a row: nothing to recover


async def test_a_grant_on_a_number_v2_left_makes_no_lead_and_keeps_its_record(
    rr, monkeypatch
):
    """v2 halted (hard stop): a grant still in bb:grants dials nothing; its bb:qi record
    stays for a manual recovery."""
    use_redis(monkeypatch, rr)
    monkeypatch.setattr(routes, "_last_mode", {})  # no memo leaks to other tests
    await granted(rr)
    await rr.hset("bb:num:N1", "mode", "legacy")
    w, calls = worker({"L1": "L1"})
    await w._round()
    assert calls == []
    assert await rr.llen("bb:tickets") == 0
    assert await rr.hget("bb:qi:T1", "L1") == "run-L1"


async def test_one_failing_entry_does_not_stop_the_batch(rr):
    await granted(rr, lines=3, members=("L1", "L2", "L3"))
    await rr.lpush("bb:grants", "not a grant")
    w, calls = worker({"L1": RuntimeError("boom"), "L2": "L2", "L3": "L3"})
    await w._round()
    assert sorted(c[1] for c in calls) == ["L1", "L2", "L3"]
    assert sorted(t.split("|")[1] for t in await rr.lrange("bb:tickets", 0, -1)) == [
        "L2",
        "L3",
    ]
    assert json.loads(await rr.hget("bb:inflight:N1", "L1"))["g"] == 1  # sent again


async def test_the_default_contract_is_the_crm_one():
    assert grants.GrantWorker()._materialize is contracts.materialize_call
    assert list(inspect.signature(contracts.materialize_call).parameters) == [
        "run_id",
        "lead_id",
        "number_id",
    ]


# --- the CRM's waiting-call facts, heard through its contract ---------------------------


def request(lead_id="L1", rank=3, order="newest_event", event_ms=1_791_522_600_123):
    return WaitingCall(
        lead_id=lead_id,
        template_id="T1",
        run_id="R1",
        ready_at=datetime.now(timezone.utc),
        rank=rank,
        order=order,
        event_at=datetime.fromtimestamp(event_ms / 1000, timezone.utc),
    )


async def test_importing_intents_registers_the_three_hooks(monkeypatch):
    monkeypatch.setattr(call_queue, "_hooks", None)
    importlib.reload(intents)
    assert call_queue._hooks == (intents.queue, intents.withdraw, intents.rerank)


async def test_queue_puts_the_call_in_its_room_with_its_rank_and_run(rr):
    await seed_number(rr, "N1", 0, {"T1": {}})
    await rr.hset("bb:num:N1", mapping={"intents": "1", "ranked": "1"})
    assert await intents.queue(request()) is True
    assert await rr.zscore("bb:q:T1", "L1") == (3 - 100) * BAND + (BAND - 1) - (
        1_791_522_600_123
    )
    assert await rr.hget("bb:qi:T1", "L1") == "R1"
    await intents.rerank("T1", "L1", 2, "newest_event", request().event_at)
    assert int((await rr.zscore("bb:q:T1", "L1")) // BAND) + 100 == 2
    await intents.withdraw("T1", "L1")
    assert not await rr.exists("bb:q:T1") and not await rr.exists("bb:qi:T1")


async def test_a_live_call_takes_its_next_day_rank_through_the_hooks(rr, monkeypatch):
    """The CRM says what a live call falls to tomorrow, on queue and on re-rank; both
    reach the queue (bb:qn remembers the next-day score)."""
    monkeypatch.setattr(
        call_queue, "_hooks", (intents.queue, intents.withdraw, intents.rerank)
    )
    await seed_number(rr, "N1", 0, {"T1": {}})
    await rr.hset("bb:num:N1", mapping={"intents": "1", "ranked": "1"})
    live = WaitingCall(
        lead_id="L1",
        template_id="T1",
        run_id="R1",
        ready_at=datetime.now(timezone.utc),
        rank=1,
        order="first_ready",
        event_at=datetime.now(timezone.utc),
        next_rank=2,
        next_order="newest_event",
    )
    assert await call_queue.call_waits(live) is True
    assert int(float(await rr.hget("bb:qn:T1", "L1")) // BAND) + 100 == 2

    assert await call_queue.call_waits(request("L2")) is True  # a pile call, rank 3
    await call_queue.call_reranked(
        "T1",
        "L2",
        1,
        "first_ready",
        datetime.now(timezone.utc),
        next_rank=3,
        next_order="newest_event",
    )
    assert int((await rr.zscore("bb:q:T1", "L2")) // BAND) + 100 == 1
    assert int(float(await rr.hget("bb:qn:T1", "L2")) // BAND) + 100 == 3


async def test_a_rerank_to_the_same_rank_keeps_a_ready_calls_place(rr):
    """First ready, first called: a live call that is ranked again (a new event, the
    same rank) is not a new arrival and stays ahead of the calls that came after it."""
    await seed_number(rr, "N1", 0, {"T1": {}})
    await rr.hset("bb:num:N1", mapping={"intents": "1", "ranked": "1"})
    first = request("L1", rank=1, order="first_ready")
    assert await intents.queue(first) is True
    place = await rr.zscore("bb:q:T1", "L1")
    await rr.zadd("bb:q:T1", {"L2": place + 5})  # a live call that arrived just after
    time.sleep(0.01)
    await intents.rerank("T1", "L1", 1, "first_ready", first.event_at)
    assert await rr.zscore("bb:q:T1", "L1") == place
    await intents.rerank("T1", "L1", 2, "first_ready", first.event_at)  # another rank
    assert int((await rr.zscore("bb:q:T1", "L1")) // BAND) + 100 == 2


async def test_queue_answers_none_when_the_number_takes_no_such_calls(rr, monkeypatch):
    await seed_number(rr, "N1", 1, {"T1": {}})  # on v2, but not an intents number
    assert await intents.queue(request()) is None
    assert not await rr.exists("bb:q:T1")

    async def never():
        return False

    monkeypatch.setattr(intents, "v2_seen", never)  # v2 never used: no Redis at all
    await rr.hset("bb:num:N1", "intents", "1")
    assert await intents.queue(request()) is None
    assert not await rr.exists("bb:q:T1")


async def test_the_batch_size_is_a_static_setting():
    from app.core.config import static

    assert grants.BATCH == static.BB_V2_GRANT_BATCH == 16


async def test_a_late_refusal_never_frees_a_line_whose_ticket_is_out(rr):
    """Two workers got the same grant (the reaper sent it again). The first made the lead
    and published its ticket; the second's CRM call then answers MOVED. Its refusal must
    not free the line: the ticket is live and a pod may be dialling it."""
    await granted(rr)
    raw = await rr.lindex("bb:grants", 0)
    first, _ = worker({"L1": "L1"})
    await first._round()
    assert await rr.llen("bb:tickets") == 1
    await rr.rpush("bb:grants", raw)  # the copy the second worker took
    second, _ = worker({"L1": Refusal.MOVED})
    await second._round()
    assert await rr.smembers("bb:busy:N1") == {"lead:L1"}
    assert await rr.hexists("bb:inflight:N1", "L1")
    assert await rr.llen("bb:tickets") == 1

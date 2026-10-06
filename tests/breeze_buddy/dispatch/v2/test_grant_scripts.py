"""A workflow call that has no lead row yet (real Redis).

On a number whose ``bb:num:{N}.intents`` is 1, ``enqueue`` may carry a run id: the
member is the id the lead WILL have, and ``bb:qi:{T}`` remembers its run. When ``match``
gives it a line, the entry goes to ``bb:grants`` (not ``bb:tickets``) and the lease
waits for its row; ``publish`` turns it into a ticket once the row exists.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import scripts
from app.ai.voice.agents.breeze_buddy.dispatch.v2.scripts import Enqueue, Rank
from tests.breeze_buddy.dispatch.v2.conftest import OWNER, seed_number

pytestmark = pytest.mark.asyncio

BAND = 10**13
EVENT = 1_791_522_600_123


def NOW() -> int:
    return int(time.time() * 1000)


async def intents_number(rr, n, lines, *, ranked=False) -> None:
    await seed_number(rr, n, lines, {"T1": {}})
    await rr.hset(
        f"bb:num:{n}",
        mapping={"intents": "1", "ranked": "1" if ranked else "0", "live_day": "0"},
    )


async def lease(rr, n, member) -> dict:
    return json.loads(await rr.hget(f"bb:inflight:{n}", member))


async def granted(rr, member="L1", run="R1", **kw) -> int:
    """Queue ``member`` for ``run`` on N1 with a free line; its ticket id."""
    assert await scripts.enqueue("T1", member, NOW() - 5, run_id=run, **kw) == 1
    return (await lease(rr, "N1", member))["tk"]


async def test_member_with_a_run_id_goes_to_grants_not_tickets(rr):
    await intents_number(rr, "N1", 1)
    tk = await granted(rr)
    assert await rr.lrange("bb:tickets", 0, -1) == []
    (entry,) = await rr.lrange("bb:grants", 0, -1)
    assert scripts.parse_grant(entry) == (
        scripts.Ticket("N1", "L1", tk, "T1", int(entry.split("|")[4])),
        "R1",
    )
    held = await lease(rr, "N1", "L1")
    assert held["g"] == 1 and held["r"] == "R1" and "ps" in held
    assert await rr.smembers("bb:busy:N1") == {"lead:L1"}
    assert await rr.hget("bb:qi:T1", "L1") == "R1"  # kept until publish or withdraw


async def test_member_without_a_run_id_is_unchanged(rr):
    await intents_number(rr, "N1", 1)
    assert await scripts.enqueue("T1", "L2", NOW() - 5) == 1
    assert await rr.lrange("bb:grants", 0, -1) == []
    assert len(await rr.lrange("bb:tickets", 0, -1)) == 1
    assert set(await lease(rr, "N1", "L2")) == {"t", "tk", "issued_ms"}
    assert not await rr.exists("bb:qi:T1")


async def test_a_run_id_needs_an_intents_number(rr):
    await seed_number(rr, "N1", 1, {"T1": {}})  # v2, but not an intents number
    assert await scripts.enqueue("T1", "L1", NOW(), run_id="R1") == Enqueue.NOT_V2
    assert not await rr.exists("bb:q:T1") and not await rr.exists("bb:qi:T1")


async def test_publish_restamps_the_issue_time_and_writes_the_ticket(rr):
    await intents_number(rr, "N1", 1)
    tk = await granted(rr)
    before = (await lease(rr, "N1", "L1"))["issued_ms"]
    await asyncio.sleep(0.01)
    assert await scripts.publish("N1", "L1", tk) == 1
    held = await lease(rr, "N1", "L1")
    assert set(held) == {"t", "tk", "issued_ms"} and held["issued_ms"] > before
    assert await rr.lrange("bb:tickets", 0, -1) == [
        f"N1|L1|{tk}|T1|{held['issued_ms']}"
    ]
    assert not await rr.hexists("bb:qi:T1", "L1")
    assert await scripts.publish("N1", "L1", tk) == 0  # once only
    assert await scripts.claim("N1", "L1", tk, OWNER)


async def test_publish_after_the_lease_is_gone_answers_0(rr):
    await intents_number(rr, "N1", 1)
    tk = await granted(rr)
    assert await scripts.reap_lease("N1", "L1", tk, "", 0) == 0
    assert await scripts.publish("N1", "L1", tk) == 0
    assert await rr.lrange("bb:tickets", 0, -1) == []


async def test_claim_and_repush_refuse_a_lease_waiting_for_its_row(rr):
    await intents_number(rr, "N1", 1)
    tk = await granted(rr)
    assert not await scripts.claim("N1", "L1", tk, OWNER)
    held = await lease(rr, "N1", "L1")
    held["issued_ms"] -= 60_000  # old enough for the reaper's unclaimed tier
    await rr.hset("bb:inflight:N1", "L1", json.dumps(held))
    assert await scripts.repush_ticket("N1", "L1", tk, 30_000, None) == 0
    assert await rr.lrange("bb:tickets", 0, -1) == []


async def test_regrant_resends_then_frees_and_requeues_at_the_same_score(rr):
    await intents_number(rr, "N1", 1, ranked=True)
    tk = await granted(rr, rank=Rank(3, "n", EVENT))
    score = (3 - 100) * BAND + (BAND - 1) - EVENT
    assert (await lease(rr, "N1", "L1"))["ps"] == str(score)
    # young: nothing; past the first age: sent again, once per window
    assert await scripts.regrant("N1", "L1", tk, 60_000, 600_000) == 0
    assert await scripts.regrant("N1", "L1", tk, 0, 600_000) == 1
    first, second = await rr.lrange("bb:grants", 0, -1)
    assert first == second and (scripts.parse_grant(first) or [])[1] == "R1"
    assert await scripts.regrant("N1", "L1", tk, 60_000, 600_000) == 0
    # past the maximum: the line is freed and the member waits again, still rank 3
    await rr.hset("bb:num:N1", "max", 0)  # so match cannot hand the line straight back
    assert await scripts.regrant("N1", "L1", tk, 0, 0) == 2
    assert not await rr.exists("bb:busy:N1") and not await rr.exists("bb:inflight:N1")
    assert await rr.zrange("bb:q:T1", 0, -1, withscores=True) == [("L1", score)]
    assert await scripts.regrant("N1", "L1", tk, 0, 0) == 0  # the lease is gone


async def test_regrant_does_not_requeue_a_withdrawn_member(rr):
    await intents_number(rr, "N1", 1)
    tk = await granted(rr)
    await scripts.withdraw("T1", "L1")
    assert await scripts.regrant("N1", "L1", tk, 0, 0) == 2
    assert not await rr.exists("bb:busy:N1") and not await rr.exists("bb:q:T1")


async def test_withdraw_clears_all_three_places(rr):
    await intents_number(rr, "N1", 0, ranked=True)
    assert (
        await scripts.enqueue(
            "T1", "L1", NOW() + 60_000, rank=Rank(2, "f", 0), run_id="R1"
        )
        == 0
    )
    for key in ("bb:q:T1", "bb:qp:T1", "bb:qi:T1"):
        assert await rr.exists(key)
    assert await scripts.withdraw("T1", "L1") == 1
    for key in ("bb:q:T1", "bb:qp:T1", "bb:qi:T1"):
        assert not await rr.exists(key)


async def test_rerank_changes_only_a_member_that_is_still_queued(rr):
    await intents_number(rr, "N1", 0, ranked=True)
    now = NOW()
    await scripts.enqueue("T1", "ready", now - 5, rank=Rank(3, "n", EVENT))
    await scripts.enqueue("T1", "later", now + 60_000, rank=Rank(3, "n", EVENT))
    assert await scripts.rerank("T1", "ready", Rank(1, "f", 0)) == 1
    assert await scripts.rerank("T1", "later", Rank(2, "n", EVENT)) == 1
    assert await scripts.rerank("T1", "gone", Rank(1, "f", 0)) == 0
    room = dict(await rr.zrange("bb:q:T1", 0, -1, withscores=True))
    assert set(room) == {"ready", "later"}
    assert int(room["ready"] // BAND) + 100 == 1  # now in the rank-1 band
    assert room["later"] == now + 60_000  # still waiting for its time
    assert await rr.hgetall("bb:qp:T1") == {
        "later": str((2 - 100) * BAND + (BAND - 1) - EVENT)
    }


async def test_rerank_leaves_an_unranked_number_alone(rr):
    await seed_number(rr, "N1", 0, {"T1": {}})
    now = NOW()
    await scripts.enqueue("T1", "L1", now - 5)
    assert await scripts.rerank("T1", "L1", Rank(1, "f", 0)) == 0
    assert await rr.zscore("bb:q:T1", "L1") == now - 5


async def test_rerank_carries_the_next_day_rank(rr):
    await intents_number(rr, "N1", 0, ranked=True)
    now = NOW()
    await scripts.enqueue("T1", "L1", now - 5, rank=Rank(1, "f", now, 3, "n"))
    assert await scripts.rerank("T1", "L1", Rank(1, "f", now, 2, "n")) == 1
    assert await rr.hget("bb:qn:T1", "L1") == str((2 - 100) * BAND + BAND - 1 - now)


async def test_a_number_that_is_not_in_mode_v2_takes_no_call_without_a_lead_row(rr):
    # switching on or off: its rooms may go to today's dialler, which needs a lead row
    await intents_number(rr, "N1", 1)
    await rr.hset("bb:num:N1", "mode", "draining")
    assert await scripts.enqueue("T1", "L1", NOW() - 5, run_id="R1") == Enqueue.NOT_V2
    assert await rr.exists("bb:qi:T1", "bb:q:T1") == 0


async def test_a_waiting_call_is_granted_after_its_number_stops_taking_new_ones(rr):
    # N1 left BB_V2_INTENT_NUMBERS: new calls get lead rows as today, but the one that
    # waits has none, so a plain ticket for it would be dropped by an acceptor
    await intents_number(rr, "N1", 0)
    await scripts.enqueue("T1", "L1", NOW() - 5, run_id="R1")
    await rr.hset("bb:num:N1", mapping={"intents": "0", "max": 1})
    assert await scripts.enqueue("T1", "L2", NOW(), run_id="R2") == Enqueue.NOT_V2
    await scripts.match("N1")
    assert await rr.lrange("bb:tickets", 0, -1) == []
    assert (scripts.parse_grant(await rr.lindex("bb:grants", 0)) or [])[1] == "R1"

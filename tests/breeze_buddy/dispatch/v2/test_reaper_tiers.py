"""The lease reaper's three tiers (spec 2026-10-05 §4.4, design card rule 14): a ticket
popped but never claimed is delivered again after 30 s; a claimed ticket that never
started dialling frees its line after 180 s from its claim and unlocks its lead; a dial
stuck for 10 min keeps rule 14."""

import json
import time
from typing import Optional
from unittest.mock import AsyncMock

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    reconcile as RC,
    routes,
    scripts,
)
from app.database.accessor.breeze_buddy.dispatch import LeadDispatchState as S
from tests.breeze_buddy.dispatch.v2.conftest import seed_number, use_redis

pytestmark = pytest.mark.asyncio


def NOW() -> int:
    return int(time.time() * 1000)


@pytest.fixture
async def rv(rr, monkeypatch):
    use_redis(monkeypatch, rr, RC, routes)
    await rr.sadd("bb:v2:active", "N1")
    yield rr


def _states(monkeypatch, status: Optional[str] = None, locked: bool = False):
    states = {} if status is None else {"L1": S(status, locked, "T1", None)}
    monkeypatch.setattr(RC, "get_lead_dispatch_states", AsyncMock(return_value=states))


async def _age(rr, field: str, ms: int) -> None:
    lease = json.loads(await rr.hget("bb:inflight:N1", "L1"))
    lease[field] = NOW() - ms
    await rr.hset("bb:inflight:N1", "L1", json.dumps(lease))


async def _popped(rr) -> scripts.Ticket:
    """L1's ticket, popped off bb:tickets by a pod that then died before claiming."""
    await seed_number(rr, "N1", 1, {"T1": {}})
    assert await scripts.enqueue("T1", "L1", NOW() - 1) == 1
    t = scripts.parse_ticket(await rr.lpop("bb:tickets"))
    assert t is not None and await rr.llen("bb:tickets") == 0
    return t


async def test_an_unclaimed_ticket_is_delivered_again_after_30_s(rv, monkeypatch):
    t = await _popped(rv)
    _states(monkeypatch)
    await _age(rv, "issued_ms", 29_000)
    assert await RC.reap_leases() == 0  # not yet
    await _age(rv, "issued_ms", 31_000)
    assert await RC.reap_leases() == 1
    again = scripts.parse_ticket(await rv.lpop("bb:tickets"))
    assert again is not None
    assert (again.number_id, again.lead_id, again.tk, again.template_id) == (
        "N1",
        "L1",
        t.tk,
        "T1",
    )  # the same ticket, on the same line
    assert await rv.sismember("bb:busy:N1", "lead:L1")
    assert await RC.reap_leases() == 0  # not again before another 30 s


async def test_no_re_delivery_while_the_kill_switch_is_off(rv, monkeypatch):
    await _popped(rv)
    _states(monkeypatch)
    await _age(rv, "issued_ms", 31_000)
    await rv.set("bb:dispatch:enabled", "0")
    assert await RC.reap_leases() == 0
    assert await rv.llen("bb:tickets") == 0


async def test_repush_racing_the_original_delivery_dials_once(rv, monkeypatch):
    t = await _popped(rv)
    _states(monkeypatch)
    await _age(rv, "issued_ms", 31_000)
    await RC.reap_leases()  # now two copies of the ticket are out
    dup = scripts.parse_ticket(await rv.lpop("bb:tickets"))
    assert dup is not None and dup.tk == t.tk
    assert await scripts.claim("N1", t.lead_id, t.tk, "first") is True
    assert await scripts.claim("N1", dup.lead_id, dup.tk, "second") is False
    second = await scripts.mark_dialling("N1", "L1", t.tk, "second")
    assert second is scripts.Mark.SUPERSEDED
    assert await scripts.mark_dialling("N1", "L1", t.tk, "first") is scripts.Mark.DIAL


async def test_a_claimed_ticket_never_dialled_frees_its_line_and_unlocks_the_lead(
    rv, monkeypatch
):
    t = await _popped(rv)
    assert await scripts.claim("N1", "L1", t.tk, "a")
    await _age(rv, "claimed_ms", 181_000)
    _states(monkeypatch, "BACKLOG", locked=True)
    unlock = AsyncMock(return_value=True)
    monkeypatch.setattr(RC, "release_lock_on_lead_by_id", unlock)
    assert await RC.reap_leases() == 1
    # re-queued (BACKLOG, due now) and matched at once: L1 holds a NEW ticket
    assert json.loads(await rv.hget("bb:inflight:N1", "L1"))["tk"] == t.tk + 1
    unlock.assert_awaited_once_with("L1")


async def test_the_claimed_tier_counts_from_the_claim_not_the_issue(rv, monkeypatch):
    # a ticket that waited 100 s for an acceptor still gets its full 180 s to dial
    t = await _popped(rv)
    await _age(rv, "issued_ms", 200_000)
    assert await scripts.claim("N1", "L1", t.tk, "a")
    _states(monkeypatch, "BACKLOG", locked=True)
    assert await RC.reap_leases() == 0
    assert json.loads(await rv.hget("bb:inflight:N1", "L1"))["tk"] == t.tk


async def test_a_lease_taken_before_this_release_counts_from_its_issue(rv, monkeypatch):
    # The previous release's take stamped an owner and no claimed_ms; such a
    # lease, live at the deploy, is reaped by its age, not skipped as unreadable forever
    t = await _popped(rv)
    assert await scripts.claim("N1", "L1", t.tk, "a")
    lease = json.loads(await rv.hget("bb:inflight:N1", "L1"))
    del lease["claimed_ms"]
    lease["issued_ms"] = NOW() - 179_000
    await rv.hset("bb:inflight:N1", "L1", json.dumps(lease))
    _states(monkeypatch, "BACKLOG", locked=True)
    monkeypatch.setattr(RC, "release_lock_on_lead_by_id", AsyncMock(return_value=True))
    assert await RC.reap_leases() == 0  # not yet
    await _age(rv, "issued_ms", 181_000)
    assert await RC.reap_leases() == 1
    assert json.loads(await rv.hget("bb:inflight:N1", "L1"))["tk"] == t.tk + 1


async def test_a_claimed_ticket_of_a_processing_lead_is_not_unlocked(rv, monkeypatch):
    t = await _popped(rv)
    assert await scripts.claim("N1", "L1", t.tk, "a")
    await _age(rv, "claimed_ms", 181_000)
    _states(monkeypatch, "PROCESSING", locked=True)
    unlock = AsyncMock(return_value=True)
    monkeypatch.setattr(RC, "release_lock_on_lead_by_id", unlock)
    await RC.reap_leases()
    unlock.assert_not_awaited()  # rule 14: a call (or a held unknown dial) owns it
    assert await rv.sismember("bb:busy:N1", "lead:L1")


async def test_a_lease_re_issued_after_the_reapers_read_is_left_alone(rv, monkeypatch):
    # the reaper's read is stale: its script finds a newer ticket's lease, changes
    # nothing (Reap.LEASE_CHANGED), and the lead's lock is that ticket's now
    t = await _popped(rv)
    assert await scripts.claim("N1", "L1", t.tk, "a")
    await _age(rv, "claimed_ms", 181_000)
    _states(monkeypatch, "BACKLOG", locked=True)
    unlock = AsyncMock(return_value=True)
    monkeypatch.setattr(RC, "release_lock_on_lead_by_id", unlock)
    real = scripts.reap_lease

    async def reissued_first(number_id, lead_id, ticket, *args, **kwargs):
        lease = json.loads(await rv.hget("bb:inflight:N1", "L1"))
        lease["tk"] = ticket + 1
        await rv.hset("bb:inflight:N1", "L1", json.dumps(lease))
        return await real(number_id, lead_id, ticket, *args, **kwargs)

    monkeypatch.setattr(scripts, "reap_lease", reissued_first)
    assert await RC.reap_leases() == 0
    unlock.assert_not_awaited()
    assert json.loads(await rv.hget("bb:inflight:N1", "L1"))["tk"] == t.tk + 1


async def test_the_script_re_pushes_at_most_once_per_window(rv):
    # the reaper's own read may be stale: the script re-checks the window itself
    t = await _popped(rv)
    await _age(rv, "issued_ms", 31_000)
    assert await scripts.repush_ticket("N1", "L1", t.tk, 30_000, None) == 1
    assert await scripts.repush_ticket("N1", "L1", t.tk, 30_000, None) == 0
    assert await scripts.repush_ticket("N1", "L1", t.tk + 1, 0, None) == 0  # another
    assert await rv.llen("bb:tickets") == 1


async def test_a_tie_with_another_numbers_head_waits_for_the_next_run(rv):
    # Tickets of one issue time are ordered by their number's ticket id; a
    # tie with another number's head tells nothing about which left the list first
    t = await _popped(rv)
    await _age(rv, "repushed_ms", 31_000)
    lease = json.loads(await rv.hget("bb:inflight:N1", "L1"))
    other = scripts.Ticket("N2", "L9", t.tk + 5, "T2", int(lease["issued_ms"]))
    assert await scripts.repush_ticket("N1", "L1", t.tk, 30_000, other) == 0
    same = scripts.Ticket("N1", "L2", t.tk + 1, "T1", int(lease["issued_ms"]))
    assert await scripts.repush_ticket("N1", "L1", t.tk, 30_000, same) == 1


async def _waiting(rr, leads: int) -> list:
    """``leads`` tickets issued a minute ago, oldest first, every one still waiting in
    bb:tickets (each pod at its in-flight guard)."""
    await seed_number(rr, "N1", leads, {"T1": {}})
    for i in range(leads):
        assert await scripts.enqueue("T1", f"L{i}", NOW() - 1) == 1
    aged = []
    for raw in await rr.lrange("bb:tickets", 0, -1):
        t = scripts.parse_ticket(raw)
        assert t is not None
        issued_ms = NOW() - 60_000 + len(aged)  # one ms apart, in issue order
        lease = json.loads(await rr.hget("bb:inflight:N1", t.lead_id))
        lease["issued_ms"] = issued_ms
        await rr.hset("bb:inflight:N1", t.lead_id, json.dumps(lease))
        aged.append(f"N1|{t.lead_id}|{t.tk}|T1|{issued_ms}")
    await rr.delete("bb:tickets")
    await rr.rpush("bb:tickets", *aged)
    return aged


async def test_tickets_still_waiting_in_the_list_are_never_doubled(rv, monkeypatch):
    entries = await _waiting(rv, 3)
    _states(monkeypatch)
    assert await RC.reap_leases() == 0
    assert await rv.lrange("bb:tickets", 0, -1) == entries


async def test_a_popped_and_lost_ticket_goes_back_to_the_head(rv, monkeypatch):
    entries = await _waiting(rv, 3)
    assert await rv.lpop("bb:tickets") == entries[0]  # its pop's reply was lost
    _states(monkeypatch)
    assert await RC.reap_leases() == 1
    assert await rv.lrange("bb:tickets", 0, -1) == entries  # oldest first, as issued


async def test_a_lease_waiting_for_its_row_is_sent_again_then_its_line_is_freed(
    rv, monkeypatch
):
    # L1 has a line but no lead row yet, and the grant worker that took its entry died
    await seed_number(rv, "N1", 1, {"T1": {}})
    await rv.hset("bb:num:N1", "intents", "1")
    assert await scripts.enqueue("T1", "L1", NOW() - 1, run_id="R1") == 1
    assert await rv.lpop("bb:grants") is not None
    tk = json.loads(await rv.hget("bb:inflight:N1", "L1"))["tk"]
    _states(monkeypatch)
    await _age(rv, "issued_ms", 4_000)
    assert await RC.reap_leases() == 0  # not yet
    await _age(rv, "issued_ms", 6_000)
    assert await RC.reap_leases() == 1  # after 5 s: its entry again, for another worker
    again = scripts.parse_grant(await rv.lpop("bb:grants"))
    assert again is not None and (again[0].tk, again[1]) == (tk, "R1")
    assert await rv.llen("bb:tickets") == 0  # never a ticket for a call with no row
    await _age(rv, "issued_ms", 31_000)
    assert await RC.reap_leases() == 1  # after 30 s: the line is freed, L1 waits again
    assert json.loads(await rv.hget("bb:inflight:N1", "L1"))["tk"] != tk  # and matched


async def test_a_line_waiting_for_its_row_raises_the_alert_by_the_leases_age(
    rv, monkeypatch
):
    """The bb:grants head is re-stamped by every re-send, so its age never grows: the
    alert reads the oldest lease still waiting for its row."""
    await seed_number(rv, "N1", 1, {"T1": {}})
    await rv.hset("bb:num:N1", "intents", "1")
    assert await scripts.enqueue("T1", "L1", NOW() - 1, run_id="R1") == 1
    _states(monkeypatch)
    alert = AsyncMock()
    monkeypatch.setattr(RC, "raise_v2_grants_waiting", alert)
    await _age(rv, "issued_ms", 4_000)
    await RC.reap_leases()
    alert.assert_not_awaited()
    await _age(rv, "issued_ms", 12_000)
    await RC.reap_leases()
    alert.assert_awaited_once()
    assert alert.await_args is not None
    number_id, age_s, waiting = alert.await_args.args
    assert (number_id, waiting) == ("N1", 1) and 11 <= age_s <= 13

"""v2 monitors (design card §5): tickets nobody takes, idle lines next to due leads, and
no sweep leader. Real Redis; alerts mocked."""

import asyncio
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import monitor as M, sweep as SW
from tests.breeze_buddy.dispatch.v2.conftest import seed_number, use_redis

pytestmark = pytest.mark.asyncio


def NOW() -> int:
    return int(time.time() * 1000)


@pytest.fixture
async def rv(rr, monkeypatch):
    use_redis(monkeypatch, rr, M)
    M._idle_last.clear()
    monkeypatch.setattr(M, "_leader_missing_since", None)
    alerts = NS(waiting=AsyncMock(), idle=AsyncMock(), leader=AsyncMock())
    monkeypatch.setattr(M, "raise_v2_tickets_waiting", alerts.waiting)
    monkeypatch.setattr(M, "raise_v2_idle_with_due_lead", alerts.idle)
    monkeypatch.setattr(M, "raise_v2_no_sweep_leader", alerts.leader)
    await seed_number(rr, "N1", 2, {"T1": {}})
    await rr.sadd("bb:v2:active", "N1")
    yield rr, alerts


async def _waiting_ticket(r, age_ms: int, claimed: bool = False) -> None:
    issued_ms = NOW() - age_ms
    lease = {"t": "T1", "issued_ms": issued_ms, "tk": 1}
    if claimed:
        lease.update(owner="o", claimed_ms=issued_ms)
    await r.hset("bb:inflight:N1", "L1", json.dumps(lease))
    await r.delete("bb:tickets")
    await r.rpush("bb:tickets", f"N1|L1|1|T1|{issued_ms}")


async def test_ticket_nobody_takes_for_10_s_alerts(rv):
    r, alerts = rv
    await _waiting_ticket(r, 3_000)
    await M.run_monitors()
    alerts.waiting.assert_not_awaited()  # young ticket
    await _waiting_ticket(r, 12_000)
    await M.run_monitors()
    alerts.waiting.assert_awaited_once()
    assert alerts.waiting.await_args_list[0].args[0] == "N1"


async def test_a_claimed_or_void_entry_at_the_head_never_alerts(rv):
    r, alerts = rv
    await _waiting_ticket(r, 12_000, claimed=True)  # a coroutine has it
    await r.lpush("bb:tickets", f"N1|GONE|7|T1|{NOW() - 60_000}")  # reaped: no lease
    await M.run_monitors()
    alerts.waiting.assert_not_awaited()


async def test_no_waiting_alert_while_the_kill_switch_is_off(rv):
    r, alerts = rv
    await _waiting_ticket(r, 12_000)
    await r.set("bb:dispatch:enabled", "0")  # the acceptors push every batch back
    await M.run_monitors()
    alerts.waiting.assert_not_awaited()


async def _void_entries(r, count: int, age_ms: int) -> None:
    """``count`` entries at the head whose leases are gone (reaped, handed back)."""
    issued_ms = NOW() - age_ms
    await r.lpush("bb:tickets", *(f"N1|GONE{i}|7|T1|{issued_ms}" for i in range(count)))


async def test_a_live_ticket_behind_a_long_void_run_still_alerts(rv):
    # The head read was 20 entries, so 20+ void entries hid a stuck ticket
    r, alerts = rv
    await _waiting_ticket(r, 12_000)
    await _void_entries(r, 2 * M.BB_V2_MONITOR_SCAN_CHUNK + 5, 60_000)
    await M.run_monitors()
    alerts.waiting.assert_awaited_once()
    # the live ticket's own wait, found past the void run (not the void head's 60 s)
    assert alerts.waiting.await_args_list[0].args == ("N1", 12)


async def test_a_void_run_longer_than_the_scan_reports_the_head_age(rv, monkeypatch):
    # nobody popped the head for 60 s: whatever waits behind it waited that long
    r, alerts = rv
    monkeypatch.setattr(M, "BB_V2_MONITOR_SCAN_CHUNK", 10)
    monkeypatch.setattr(M, "BB_V2_MONITOR_SCAN_MAX", 20)
    await _waiting_ticket(r, 3_000)
    await _void_entries(r, 25, 60_000)
    await M.run_monitors()
    alerts.waiting.assert_awaited_once()
    assert alerts.waiting.await_args_list[0].args[1] >= 59


async def test_a_number_unmatched_past_its_due_time_alerts_on_the_second_check(rv):
    r, alerts = rv
    await r.zadd("bb:due", {"N1": NOW() - 6_000})  # every match rewrites it to now+
    await M.run_monitors()
    alerts.idle.assert_not_awaited()
    await M.run_monitors()
    alerts.idle.assert_awaited_once_with("N1")


async def test_a_number_matched_in_between_does_not_alert(rv):
    r, alerts = rv
    await r.zadd("bb:due", {"N1": NOW() - 6_000})
    await M.run_monitors()
    await r.zadd("bb:due", {"N1": NOW()})  # a tick matched it
    await M.run_monitors()
    alerts.idle.assert_not_awaited()


@pytest.mark.parametrize("why", ["young", "pending", "draining", "kill_switch"])
async def test_an_entry_left_on_purpose_never_alerts(rv, why):
    r, alerts = rv
    await r.zadd("bb:due", {"N1": NOW() - (1_000 if why == "young" else 60_000)})
    if why in ("pending", "draining"):
        # match leaves the entry untouched until the mode is v2
        await r.hset("bb:num:N1", "mode", {"pending": "v2_pending"}.get(why, why))
    elif why == "kill_switch":
        await r.set("bb:dispatch:enabled", "0")
    await M.run_monitors()
    await M.run_monitors()
    alerts.idle.assert_not_awaited()


@pytest.mark.parametrize("blocker", ["full", "later", "paused", "disabled", "closed"])
async def test_a_lead_match_would_not_take_never_alerts(rv, blocker):
    # match itself writes bb:due: never in the past for a lead it cannot take now
    r, alerts = rv
    due = NOW() + 60_000 if blocker == "later" else NOW() - 60_000
    if blocker == "full":
        await r.sadd("bb:busy:N1", "lead:a", "lead:b")
    elif blocker == "paused":
        await r.set("bb:reseller:paused:R1", "1")  # today's key, no mirror (Fable M2)
    elif blocker == "disabled":
        await r.hset("bb:route:T1", "enabled", "0")
    elif blocker == "closed":
        now_ist = (int(time.time()) + 19800) % 86400
        await r.hset(
            "bb:route:T1",
            mapping={
                "start": (now_ist + 3600) % 86400,
                "end": (now_ist + 7200) % 86400,
            },
        )
    assert await SW.scripts.enqueue("T1", "L1", due) == 0
    await M.run_monitors()
    await M.run_monitors()
    alerts.idle.assert_not_awaited()


async def test_no_sweep_leader_for_10_s_alerts(rv, monkeypatch):
    r, alerts = rv
    clock = [100.0]
    monkeypatch.setattr(M.time, "monotonic", lambda: clock[0])
    await M.check_sweep_leader()  # first miss: start the clock
    clock[0] = 109.0
    await M.check_sweep_leader()
    alerts.leader.assert_not_awaited()
    clock[0] = 110.0
    await M.check_sweep_leader()
    alerts.leader.assert_awaited_once()
    await r.set("bb:v2:sweep:leader", "pod-1")
    await M.check_sweep_leader()  # a leader again: reset
    assert M._leader_missing_since is None


async def test_sweeper_runs_monitors_every_15_ticks_and_leader_check_on_every_pod(
    monkeypatch,
):
    assert {job.name: job.every for job in SW.JOBS}["monitor"] == 15
    monkeypatch.setattr(SW, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(SW, "SWEEP_INTERVAL_S", 0.001)
    check = AsyncMock()
    monkeypatch.setattr(SW, "check_sweep_leader", check)
    sw = SW.Sweeper(redis_client=MagicMock())
    monkeypatch.setattr(
        sw, "_leader", NS(is_leader=False, start=AsyncMock(), stop=AsyncMock())
    )
    sw.start()
    for _ in range(200):
        if check.await_count:
            break
        await asyncio.sleep(0.005)
    await sw.stop()
    assert check.await_count >= 1  # a non-leader pod watches the leader

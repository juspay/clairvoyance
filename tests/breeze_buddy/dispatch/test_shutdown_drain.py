"""Worker.stop(): drain a dial that is on the wire, never cancel it.

A dial inside or past ``make_call`` may already have rung the customer's
phone. Cancelling it leaves ``make_call`` running in its thread while the
``finally`` unlocks a lead whose call exists (re-dial) and the token + DB
channel leak. ``stop()`` therefore waits for such a dial to finish; only an
idle worker (BLPOP) or one still in the pre-dial checks is cancelled.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from app.ai.voice.agents.breeze_buddy.dispatch import worker as w
from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    channel_tokens_available,
    init_channel_semaphore,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    READY_LIST,
    SCHEDULE_ZSET,
    worker_heartbeat_key,
)
from app.schemas import LeadCallStatus
from tests.breeze_buddy.dispatch.conftest import make_lead


def _patch_blpop(monkeypatch, fake_redis):
    """A BLPOP that pops a ready lead, else parks until cancelled (the real
    one blocks in Redis; the fake returns at once and would spin the loop)."""

    async def _blpop(self):
        lst = fake_redis.client.lists.get(READY_LIST, [])
        if lst:
            return lst.pop(0)
        await asyncio.sleep(3600)

    monkeypatch.setattr(w.Worker, "_blpop_ready", _blpop)


def _gate_make_call(harness, monkeypatch):
    """make_call parks until the test opens the gate."""
    started, gate = asyncio.Event(), asyncio.Event()
    real = harness.call_recorder.make_call_async

    async def _dial(*args, **kwargs):
        started.set()
        await gate.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(harness.call_recorder, "make_call_async", _dial)
    return started, gate


async def _start(harness, fake_redis, monkeypatch, lead_id="lead-drain"):
    _patch_blpop(monkeypatch, fake_redis)
    lead = make_lead(lead_id)
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)
    worker = w.Worker(worker_uuid="w-drain")
    await worker.start()
    return worker, lead


async def test_stop_waits_for_an_in_flight_make_call(harness, fake_redis, monkeypatch):
    started, gate = _gate_make_call(harness, monkeypatch)
    worker, lead = await _start(harness, fake_redis, monkeypatch)
    await asyncio.wait_for(started.wait(), 2)

    stopping = asyncio.create_task(worker.stop())
    await asyncio.sleep(0.2)
    # Still draining: not cancelled, nothing given back, lead not re-queued.
    assert not stopping.done()
    assert harness.released_locks == []
    assert harness.deferred == []
    assert harness.released_numbers == []

    gate.set()
    await asyncio.wait_for(stopping, 2)

    # The dial completed normally: the call owns the line and the lead.
    assert len(harness.call_recorder.calls) == 1
    assert lead.status == LeadCallStatus.PROCESSING
    assert lead.call_id == "CA-test-sid"
    assert await channel_tokens_available(harness.number.id) == 0
    assert harness.released_numbers == []
    assert harness.released_locks == []  # the call-end webhook releases it
    assert harness.deferred == []


async def test_stop_never_cancels_a_dial_past_its_budget(
    harness, fake_redis, monkeypatch
):
    monkeypatch.setattr(w, "BB_WORKER_SHUTDOWN_DRAIN_S", 0.3)
    started, gate = _gate_make_call(harness, monkeypatch)
    worker, lead = await _start(harness, fake_redis, monkeypatch)
    task = worker._task
    await asyncio.wait_for(started.wait(), 2)

    t0 = time.monotonic()
    await asyncio.wait_for(worker.stop(), 2)
    assert time.monotonic() - t0 < 1.5  # bounded

    # Gave up waiting, did NOT cancel: no unlock, no defer, no release.
    assert task is not None and not task.cancelled() and not task.done()
    assert harness.released_locks == []
    assert harness.deferred == []
    assert harness.released_numbers == []

    gate.set()  # let it finish so the test leaves nothing running
    await asyncio.wait_for(asyncio.shield(task), 2)
    assert lead.status == LeadCallStatus.PROCESSING


async def test_stop_returns_promptly_for_an_idle_worker(
    harness, fake_redis, monkeypatch
):
    _patch_blpop(monkeypatch, fake_redis)
    worker = w.Worker(worker_uuid="w-idle")
    await worker.start()
    await asyncio.sleep(0.05)

    t0 = time.monotonic()
    await asyncio.wait_for(worker.stop(), 2)
    assert time.monotonic() - t0 < 1.0
    assert worker._task is None


async def test_stop_cancels_a_worker_still_in_pre_dial_checks(
    harness, fake_redis, monkeypatch
):
    """Before the commit point nothing is on the wire: cancel, and the lead's
    lock goes back so it is picked up again."""
    reached, never = asyncio.Event(), asyncio.Event()

    async def _pre_checks(*args, **kwargs):
        reached.set()
        await never.wait()

    monkeypatch.setattr(w, "_run_pre_checks_for_lead", _pre_checks)
    worker, lead = await _start(harness, fake_redis, monkeypatch)
    await asyncio.wait_for(reached.wait(), 2)

    await asyncio.wait_for(worker.stop(), 2)

    assert harness.call_recorder.calls == []
    assert lead.id in harness.released_locks
    assert lead.status == LeadCallStatus.BACKLOG
    # popped off the ready list, so it must be back on the schedule (the
    # backlog reconciler only sees the oldest due rows: under a pile it would
    # not find this lead for a long time)
    assert lead.id in fake_redis.client.zsets.get(SCHEDULE_ZSET, {})


async def test_stop_waits_while_the_channel_is_being_taken(
    harness, fake_redis, monkeypatch
):
    """A cancel between taking the channel and the give-back paths would leak
    it: stop() waits through those short steps, the dial goes ahead and is
    drained, and nothing leaks."""
    reached, gate = asyncio.Event(), asyncio.Event()
    real_acquire = w._acquire_number

    async def _slow_acquire(number):
        reached.set()
        await gate.wait()
        return await real_acquire(number)

    monkeypatch.setattr(w, "_acquire_number", _slow_acquire)
    worker, lead = await _start(harness, fake_redis, monkeypatch)
    await asyncio.wait_for(reached.wait(), 2)

    stopping = asyncio.create_task(worker.stop())
    await asyncio.sleep(0.6)
    assert not stopping.done()  # waiting, not cancelling
    gate.set()
    await asyncio.wait_for(stopping, 5)

    assert len(harness.call_recorder.calls) == 1
    assert lead.status == LeadCallStatus.PROCESSING
    assert harness.released_numbers == []


async def test_stop_during_greeting_prewarm_gives_the_channel_back_and_requeues(
    harness, fake_redis, monkeypatch
):
    reached, never = asyncio.Event(), asyncio.Event()

    async def _prewarm(*args, **kwargs):
        reached.set()
        await never.wait()

    async def _template(template_id):
        return SimpleNamespace(id="tmpl-1", is_active=True)

    monkeypatch.setattr(w, "get_template_by_id", _template)
    monkeypatch.setattr(w, "_prewarm_initial_greeting_with_retry", _prewarm)
    worker, lead = await _start(harness, fake_redis, monkeypatch)
    await asyncio.wait_for(reached.wait(), 2)

    await asyncio.wait_for(worker.stop(), 2)

    assert harness.call_recorder.calls == []
    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.released_numbers == [harness.number.id]
    assert lead.status == LeadCallStatus.BACKLOG
    assert lead.id in fake_redis.client.zsets.get(SCHEDULE_ZSET, {})


async def test_heartbeat_keeps_beating_while_a_dial_drains(
    harness, fake_redis, monkeypatch
):
    """A silent heartbeat during the drain would let the reaper requeue the
    lead that is mid-dial."""
    monkeypatch.setattr(w, "BB_WORKER_HEARTBEAT_REFRESH_S", 0.05)
    started, gate = _gate_make_call(harness, monkeypatch)
    worker, _ = await _start(harness, fake_redis, monkeypatch)
    await asyncio.wait_for(started.wait(), 2)

    stopping = asyncio.create_task(worker.stop())
    await asyncio.sleep(0.1)
    key = worker_heartbeat_key("w-drain")
    fake_redis.client.kv.pop(key, None)
    await asyncio.sleep(0.2)
    assert key in fake_redis.client.kv

    gate.set()
    await asyncio.wait_for(stopping, 2)


async def test_stop_while_the_popped_lead_is_being_tracked_puts_it_back(
    harness, fake_redis, monkeypatch
):
    """The lead is already off the ready list and the phase is cancellable
    while its processing-list RPUSH runs: a stop landing there must still put
    it back on the schedule, or it sits in no list until a reconciler finds it."""
    reached, never = asyncio.Event(), asyncio.Event()

    async def _slow_track(self, lead_id):
        reached.set()
        await never.wait()

    monkeypatch.setattr(w.Worker, "_rpush_processing", _slow_track)
    worker, lead = await _start(harness, fake_redis, monkeypatch)
    await asyncio.wait_for(reached.wait(), 2)

    await asyncio.wait_for(worker.stop(), 2)

    assert harness.call_recorder.calls == []
    assert fake_redis.client.lists.get(READY_LIST, []) == []
    assert lead.status == LeadCallStatus.BACKLOG
    assert lead.id in fake_redis.client.zsets.get(SCHEDULE_ZSET, {})

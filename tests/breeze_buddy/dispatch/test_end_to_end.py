"""
End-to-end dispatch round-trip tests.

Exercises the full hot path that unit tests don't cover:

    schedule_lead -> SCHEDULE_ZSET -> promoter (Lua) -> READY_LIST
      -> worker BLPOP -> processing list -> DB CAS lock -> pre-checks
      -> rate limit -> number pick -> channel BLPOP -> make_call
      -> post-CAS UPDATE -> processing list LREM

Worker collaborators (DB accessors, managers.calls helpers, telephony
provider, greeting prep) come from the shared ``DispatchHarness`` in
``conftest.py``. Redis is the in-memory fake.

Scope: chain correctness, ordering, and resource accounting (channel
tokens, DB locks). Out of scope: actual Lua execution semantics, real
Redis cluster behaviour, provider QPS handling.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import cast

from app.ai.voice.agents.breeze_buddy.dispatch import (
    promoter as prom_mod,
    reconcilers as recon_mod,
    worker as w,
)
from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    channel_tokens_available,
    init_channel_semaphore,
    release_channel_token,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    READY_LIST,
    SCHEDULE_ZSET,
    processing_list_for,
    worker_heartbeat_key,
)
from app.ai.voice.agents.breeze_buddy.dispatch.leader import LeaderElection
from app.ai.voice.agents.breeze_buddy.dispatch.queue import schedule_lead
from app.schemas import LeadCallStatus
from tests.breeze_buddy.dispatch.conftest import (
    AlwaysLeader,
    CallRecorder,
    make_lead,
)

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_full_round_trip_happy_path(harness, fake_redis):
    """
    Schedule → promote → BLPOP → dispatch → make_call → CAS update.
    Verifies the entire chain executes and channel token is consumed.
    """
    lead = make_lead("lead-happy")
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 2)

    # ZADD with score in the past so the promoter picks it up.
    await schedule_lead(lead.id, datetime.now(timezone.utc) - timedelta(seconds=1))
    assert await fake_redis.client.zcard(SCHEDULE_ZSET) == 1

    # Run promoter once with always-leader stub. Pyrefly: structural stand-in
    # is fine at runtime; cast for the nominal type.
    promoter = prom_mod.Promoter(leader=cast(LeaderElection, AlwaysLeader()))
    moved = await promoter._tick_once()
    assert moved == 1
    assert await fake_redis.client.zcard(SCHEDULE_ZSET) == 0
    assert await fake_redis.client.llen(READY_LIST) == 1

    # Drive one worker iteration.
    worker = w.Worker(worker_uuid="w-happy")
    await worker._iteration(session=None)

    # Lead was dialled exactly once.
    assert len(harness.call_recorder.calls) == 1
    assert harness.call_recorder.calls[0]["to"] == "+15551234567"
    assert harness.call_recorder.calls[0]["from"] == "+15559999999"

    # Status was advanced.
    assert lead.status == LeadCallStatus.PROCESSING
    assert lead.call_id == "CA-test-sid"
    assert lead.outbound_number_id == harness.number.id

    # Channel token was consumed (2 → 1) and not yet returned (waiting on webhook).
    assert await channel_tokens_available(harness.number.id) == 1

    # Processing list is cleaned up (LREM ran in finally).
    assert await fake_redis.client.llen(processing_list_for("w-happy")) == 0

    # Ready list drained.
    assert await fake_redis.client.llen(READY_LIST) == 0

    # Lock was NOT released by the worker (waiting on call-end webhook).
    assert "lead-happy" not in harness.released_locks


# ---------------------------------------------------------------------------
# Failure branches
# ---------------------------------------------------------------------------


async def test_make_call_exception_releases_token_and_defers(harness, fake_redis):
    """provider.make_call raising → channel token returned, number released, defer scheduled."""
    lead = make_lead("lead-exc")
    harness.add_lead(lead)
    harness.call_recorder = CallRecorder(raise_exc=RuntimeError("twilio down"))
    # Re-bind get_voice_provider so it returns the new recorder.
    w.get_voice_provider = harness.get_voice_provider

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-exc")
    await worker._iteration(session=None)

    # No call recorded.
    assert harness.call_recorder.calls == []

    # Channel token returned (1 → consumed in BLPOP → put back → 1 again).
    assert await channel_tokens_available(harness.number.id) == 1

    # DB-side number was released too.
    assert harness.released_numbers == [harness.number.id]

    # Lead was deferred with backoff (5 * (0 + 1) = 5s).
    assert harness.deferred == [(lead.id, 5)]

    # Lead is still BACKLOG (no PROCESSING update happened).
    assert lead.status == LeadCallStatus.BACKLOG


async def test_make_call_returns_no_sid_defers(harness, fake_redis):
    """make_call returning {} → token and number released, lead deferred."""
    lead = make_lead("lead-nosid")
    harness.add_lead(lead)
    harness.call_recorder = CallRecorder(sid=None)
    w.get_voice_provider = harness.get_voice_provider

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-nosid")
    await worker._iteration(session=None)

    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.released_numbers == [harness.number.id]
    assert harness.deferred == [(lead.id, 10)]
    assert lead.status == LeadCallStatus.BACKLOG


async def test_post_cas_lost_releases_all_resources(harness, fake_redis):
    """
    CAS lost after make_call → token returned, number released, lock released.
    The call is placed but the lead row already moved on (e.g., aborted).
    """
    lead = make_lead("lead-cas")
    harness.add_lead(lead)
    harness.cas_succeeds = False

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-cas")
    await worker._iteration(session=None)

    # Call WAS placed (CAS happens after make_call).
    assert len(harness.call_recorder.calls) == 1

    # All resources cleaned up despite orphaning the call.
    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.released_numbers == [harness.number.id]
    assert lead.id in harness.released_locks


async def test_status_not_backlog_skips_dispatch(harness, fake_redis):
    """Lead already in PROCESSING → worker drops without acquiring channel."""
    lead = make_lead("lead-skip", status=LeadCallStatus.PROCESSING)
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-skip")
    await worker._iteration(session=None)

    # No call, no resource consumption, no defer.
    assert harness.call_recorder.calls == []
    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.deferred == []
    # No lock attempted (the status check happens before acquire_lock).
    assert lead.id not in harness.released_locks


async def test_lock_acquire_fails_drops_lead(harness, fake_redis):
    """
    Another worker holds the lock → acquire returns None → drop cleanly.
    No channel consumed, no defer.
    """
    lead = make_lead("lead-locked")
    harness.add_lead(lead)
    harness.locked_lead_ids.add(lead.id)  # simulate another worker holds it.
    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-locked")
    await worker._iteration(session=None)

    assert harness.call_recorder.calls == []
    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.deferred == []


async def test_rate_limit_blocks_before_channel_acquire(harness, fake_redis):
    """
    Rate-limit deny → lead deferred WITHOUT holding a channel token.
    Critical invariant: rate-limit check runs BEFORE channel BLPOP so a
    rate-limited lead doesn't block other leads on the same number.
    """
    lead = make_lead("lead-rl")
    harness.add_lead(lead)
    harness.rate_limit_ok = False
    harness.rate_limit_defer_seconds = 30

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-rl")
    await worker._iteration(session=None)

    # Channel token NEVER consumed — still 1 available.
    assert await channel_tokens_available(harness.number.id) == 1
    # No call, no number released (never acquired), defer recorded.
    assert harness.call_recorder.calls == []
    assert harness.released_numbers == []
    assert harness.deferred == [(lead.id, 30)]


async def test_blacklisted_phone_finalizes_lead(harness, fake_redis):
    """Blacklisted phone → lead FINISHED with BLACKLISTED outcome, no call."""
    lead = make_lead("lead-bl")
    harness.add_lead(lead)
    harness.is_blacklisted = True

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-bl")
    await worker._iteration(session=None)

    assert harness.call_recorder.calls == []
    assert lead.status == LeadCallStatus.FINISHED
    assert lead.outcome == "BLACKLISTED"
    assert await channel_tokens_available(harness.number.id) == 1


async def test_get_available_number_returns_none_defers_with_backoff(
    harness, fake_redis
):
    """No outbound number free → short defer, no channel consumed."""
    lead = make_lead("lead-nonum")
    harness.add_lead(lead)
    harness.get_available_returns_none = True

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-nonum")
    await worker._iteration(session=None)

    assert harness.call_recorder.calls == []
    assert harness.deferred == [(lead.id, 10)]
    assert await channel_tokens_available(harness.number.id) == 1


# ---------------------------------------------------------------------------
# Channel token return via webhook path
# ---------------------------------------------------------------------------


async def test_channel_token_returns_on_release(harness, fake_redis):
    """
    Worker consumes a token (happy path), then a webhook handler calls
    release_channel_token → token count back to original.
    """
    lead = make_lead("lead-return")
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 2)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-return")
    await worker._iteration(session=None)

    assert lead.status == LeadCallStatus.PROCESSING
    assert await channel_tokens_available(harness.number.id) == 1  # one consumed

    # Simulate the call-end webhook returning a token.
    await release_channel_token(harness.number.id)
    assert await channel_tokens_available(harness.number.id) == 2  # restored


# ---------------------------------------------------------------------------
# Reaper recovers a worker that "crashed" mid-dispatch
# ---------------------------------------------------------------------------


async def test_reaper_recovers_stuck_processing_lead(harness, fake_redis, monkeypatch):
    """
    Simulate: worker RPUSHes processing-list entry, then dies without LREM
    or heartbeat refresh. Reaper runs → re-ZADDs the lead onto SCHEDULE.
    """
    lead = make_lead("lead-stuck")
    harness.add_lead(lead)

    # Patch reconcilers' DB dep too — get_lead_by_id is imported there.
    monkeypatch.setattr(recon_mod, "get_lead_by_id", harness.get_lead_by_id)

    # Worker grabbed the lead and tracked it, but never finished.
    worker_uuid = "w-crashed"
    proc_key = processing_list_for(worker_uuid)
    await fake_redis.client.rpush(proc_key, lead.id)
    # No heartbeat — simulates dead worker.
    assert worker_heartbeat_key(worker_uuid) not in fake_redis.client.kv

    # Schedule is empty before reaper runs.
    assert await fake_redis.client.zcard(SCHEDULE_ZSET) == 0

    await recon_mod.reap_stuck_processing_lists()

    # Lead is back on the schedule, processing-list entry cleared.
    assert await fake_redis.client.zcard(SCHEDULE_ZSET) == 1
    assert lead.id in fake_redis.client.zsets[SCHEDULE_ZSET]
    assert (
        proc_key not in fake_redis.client.lists
        or fake_redis.client.lists[proc_key] == []
    )


async def test_reaper_skips_alive_worker(harness, fake_redis, monkeypatch):
    """Worker heartbeat still present → reaper leaves the entry alone."""
    lead = make_lead("lead-alive")
    harness.add_lead(lead)
    monkeypatch.setattr(recon_mod, "get_lead_by_id", harness.get_lead_by_id)

    worker_uuid = "w-alive"
    proc_key = processing_list_for(worker_uuid)
    await fake_redis.client.rpush(proc_key, lead.id)
    # Heartbeat present.
    fake_redis.client.kv[worker_heartbeat_key(worker_uuid)] = "1"

    await recon_mod.reap_stuck_processing_lists()

    # Untouched.
    assert await fake_redis.client.zcard(SCHEDULE_ZSET) == 0
    assert fake_redis.client.lists[proc_key] == [lead.id]


async def test_reaper_drops_already_processing_lead(harness, fake_redis, monkeypatch):
    """
    Worker died, but the call was already placed (lead is in PROCESSING).
    The reaper should clean the tracking entry but NOT re-schedule.
    """
    lead = make_lead("lead-already", status=LeadCallStatus.PROCESSING)
    harness.add_lead(lead)
    monkeypatch.setattr(recon_mod, "get_lead_by_id", harness.get_lead_by_id)

    worker_uuid = "w-zombie"
    proc_key = processing_list_for(worker_uuid)
    await fake_redis.client.rpush(proc_key, lead.id)
    # No heartbeat.

    await recon_mod.reap_stuck_processing_lists()

    # Schedule untouched (call already in flight), tracking cleaned.
    assert await fake_redis.client.zcard(SCHEDULE_ZSET) == 0
    assert (
        proc_key not in fake_redis.client.lists
        or fake_redis.client.lists[proc_key] == []
    )

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
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    OutboundCallContext,
    OutboundCallPlacement,
)
from app.core.config.static import BB_CHANNEL_WAIT_BACKOFF_MAX_S
from app.schemas import CallProvider, LeadCallStatus
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
    assert harness.call_recorder.calls[0]["to"] == "+14155552671"
    assert harness.call_recorder.calls[0]["from"] == "+15559999999"

    # Status was advanced.
    assert lead.status == LeadCallStatus.PROCESSING
    assert lead.call_id == "CA-test-sid"
    assert lead.telephony_number_id == harness.number.id

    # Channel token was consumed (2 → 1) and not yet returned (waiting on webhook).
    assert await channel_tokens_available(harness.number.id) == 1

    # Processing list is cleaned up (LREM ran in finally).
    assert await fake_redis.client.llen(processing_list_for("w-happy")) == 0

    # Ready list drained.
    assert await fake_redis.client.llen(READY_LIST) == 0

    # Lock was NOT released by the worker (waiting on call-end webhook).
    assert "lead-happy" not in harness.released_locks

    # Rate-limit accounting: peek runs once (before channel), record runs
    # once (only after make_call success). The peek-vs-record split is the
    # invariant that fixed the spurious-BLOCKED-alert regression.
    assert len(harness.rate_limit_peeks) == 1
    assert len(harness.rate_limit_records) == 1
    assert harness.rate_limit_records[0]["lead_id"] == "lead-happy"


async def test_atomic_record_rejection_releases_resources_and_defers(
    harness, fake_redis
):
    """
    Cross-lead race regression guard.

    Scenario: this worker's peek saw count < max_calls (the bucket had
    room), but between the peek and the atomic-record point another
    concurrent worker on the same customer phone won the race and filled
    the bucket. Our atomic-record returns rejected.

    Required behavior — restores the pre-PR strict cap that the initial
    peek/record split silently relaxed:

      - Channel token MUST be released (so other leads on this number
        aren't starved by a rejected-but-still-holding-the-slot worker).
      - DB telephony_number MUST be released (mirror of the channel
        release; keeps the operator-visible counter consistent).
      - Lead MUST be deferred by the rate-limit window (default 3600s)
        so the dispatcher doesn't immediately re-pick and burn through
        the next window of attempts.
      - provider.make_call MUST NOT run — the whole point of putting
      the atomic gate before make_call is so the customer's phone
      never rings on a race-loss.
    """
    lead = make_lead("lead-race")
    harness.add_lead(lead)
    # Peek under-limit, atomic-record rejected (race with another worker).
    harness.rate_limit_ok = True
    harness.rate_limit_record_accepts = False
    harness.rate_limit_record_defer_seconds = 3600  # = window_seconds default

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-race")
    await worker._iteration(session=None)

    # No dial — make_call MUST NOT have run.
    assert harness.call_recorder.calls == []
    # Channel token restored to the pool (released after rejection).
    assert await channel_tokens_available(harness.number.id) == 1
    # DB number released too.
    assert harness.released_numbers == [harness.number.id]
    # Deferred by the rate-limit window.
    assert harness.deferred == [(lead.id, 3600)]
    # Peek ran once (allowed), record ran once (rejected).
    assert len(harness.rate_limit_peeks) == 1
    assert len(harness.rate_limit_records) == 1
    # Lead is still BACKLOG (deferred, not finalized).
    assert lead.status == LeadCallStatus.BACKLOG


async def test_channel_exhaustion_does_not_record_rate_limit_attempt(
    harness, fake_redis
):
    """
    Regression guard for the BLOCKED-alert storm of 2026-05-21.

    When every channel token is held by other in-flight calls, the worker
    must defer WITHOUT having ZADDed the sliding-window bucket. Previously,
    the rate-limit ZADD ran before ``acquire_channel_token``, so a single
    lead retrying every 1-3s on exhaustion would self-fill its own bucket
    in ~10 seconds, fire a false-positive Slack alert, and get pushed out
    by 3600s — all without ever placing a call.

    The fix splits peek (read-only, runs every dispatch) from record (ZADD,
    runs only after provider.make_call succeeds). This test pins the
    invariant: no call → no record, ever, regardless of how many times
    the worker bounces on capacity.
    """
    lead = make_lead("lead-exhausted")
    harness.add_lead(lead)

    # Initialise with zero tokens — pretend the number is fully saturated.
    await init_channel_semaphore(harness.number.id, 0)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-exhausted")
    await worker._iteration(session=None)

    # No call, no number acquired, lead deferred with channel-wait jitter.
    assert harness.call_recorder.calls == []
    assert harness.released_numbers == []
    assert len(harness.deferred) == 1
    deferred_lead, defer_seconds = harness.deferred[0]
    assert deferred_lead == "lead-exhausted"
    # Defer is the channel-wait backoff: random.randint(1, MAX).
    # Asserting against the actual config value (vs. a loose upper bound)
    # so this test fails fast if the bound is ever silently tightened.
    assert 1 <= defer_seconds <= BB_CHANNEL_WAIT_BACKOFF_MAX_S

    # The critical invariant — peek can run, record must not.
    assert len(harness.rate_limit_peeks) == 1
    assert harness.rate_limit_records == []


# ---------------------------------------------------------------------------
# Failure branches
# ---------------------------------------------------------------------------


async def test_invalid_legacy_phone_is_terminal_without_provider_call(
    harness, fake_redis
):
    lead = make_lead("lead-invalid-phone", phone="not-a-phone")
    harness.add_lead(lead)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-invalid-phone")
    await worker._iteration(session=None)

    assert lead.status == LeadCallStatus.FINISHED
    assert lead.outcome == "INVALID_PHONE"
    assert harness.call_recorder.calls == []
    assert harness.rate_limit_peeks == []
    assert harness.rate_limit_records == []
    assert harness.released_numbers == []
    assert harness.deferred == []
    assert harness.released_locks == [lead.id]


async def test_invalid_legacy_phone_terminal_update_failure_keeps_lock_held(
    harness, fake_redis
):
    lead = make_lead("lead-invalid-terminal-failure", phone="not-a-phone")
    harness.add_lead(lead)
    harness.finish_and_release_succeeds = False
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-invalid-terminal-failure")
    await worker._iteration(session=None)

    assert lead.status == LeadCallStatus.BACKLOG
    assert lead.outcome is None
    assert lead.id in harness.locked_lead_ids
    assert harness.released_locks == []
    assert harness.deferred == []
    assert harness.call_recorder.calls == []


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


async def test_plivo_submission_ack_waits_for_callback_without_no_sid_retry(
    harness, fake_redis
):
    lead = make_lead("lead-plivo-ack")
    harness.add_lead(lead)
    harness.number.provider = CallProvider.PLIVO
    harness.config.calling_provider = CallProvider.PLIVO
    harness.call_recorder = CallRecorder(
        response=OutboundCallPlacement.submitted("rq-test", api_id="api-test")
    )
    w.get_voice_provider = harness.get_voice_provider

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-plivo-ack")
    await worker._iteration(session=None)

    assert harness.call_recorder.calls == [
        {
            "to": "+14155552671",
            "from": "+15559999999",
            "reseller_id": "res-1",
            "template_name": "tmpl-1",
            "context": OutboundCallContext(
                reseller_id="res-1",
                template_id="tmpl-1",
                lead_id=lead.id,
                telephony_number_id=harness.number.id,
            ),
        }
    ]
    assert lead.status == LeadCallStatus.PROCESSING
    assert lead.call_id == "rq-test"
    assert lead.telephony_number_id == harness.number.id
    assert lead.id in harness.locked_lead_ids
    assert harness.deferred == []
    assert harness.released_numbers == []
    assert await channel_tokens_available(harness.number.id) == 0
    assert harness.metadata_updates == [
        (
            lead.id,
            {
                "provider_call_submission": {
                    "provider": CallProvider.PLIVO.value,
                    "telephony_number_id": harness.number.id,
                    "request_uuid": "rq-test",
                    "api_id": "api-test",
                    "status": "accepted_waiting_for_call_uuid",
                }
            },
        )
    ]


async def test_plivo_submission_mark_failure_defers_without_redial_window(
    harness, fake_redis
):
    lead = make_lead("lead-plivo-mark-fail")
    harness.add_lead(lead)
    harness.number.provider = CallProvider.PLIVO
    harness.config.calling_provider = CallProvider.PLIVO
    harness.call_recorder = CallRecorder(
        response=OutboundCallPlacement.submitted("rq-test", api_id="api-test")
    )
    harness.mark_provider_submission_succeeds = False
    w.get_voice_provider = harness.get_voice_provider

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-plivo-mark-fail")
    await worker._iteration(session=None)

    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.released_numbers == [harness.number.id]
    assert harness.released_locks == []
    assert harness.deferred == [(lead.id, 60)]
    assert lead.status == LeadCallStatus.BACKLOG
    assert lead.id not in harness.locked_lead_ids


async def test_plivo_ambiguous_timeout_is_terminal_without_blind_retry(
    harness, fake_redis
):
    lead = make_lead("lead-plivo-timeout")
    harness.add_lead(lead)
    harness.number.provider = CallProvider.PLIVO
    harness.config.calling_provider = CallProvider.PLIVO
    harness.call_recorder = CallRecorder(
        response=OutboundCallPlacement.unknown(
            "read timed out", error_type="ReadTimeout"
        )
    )
    w.get_voice_provider = harness.get_voice_provider

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-plivo-timeout")
    await worker._iteration(session=None)

    assert lead.status == LeadCallStatus.FINISHED
    assert lead.outcome == "PLACEMENT_UNKNOWN"
    assert harness.deferred == []
    assert harness.released_numbers == [harness.number.id]
    assert await channel_tokens_available(harness.number.id) == 1


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
    Rate-limit deny → lead deferred WITHOUT holding a channel token and
    WITHOUT recording the attempt against the sliding window.

    Critical invariants (post-PR-#776 split into peek + record):
      - peek runs before channel BLPOP so a rate-limited lead doesn't
        hold a channel token while it's bouncing.
      - peek does NOT mutate the ZSET; only record does, and record runs
        only after a successful provider.make_call.
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
    # Peek ran exactly once; record never ran (this is the bug we fixed —
    # the old code would have ZADDed here even though no call went out).
    assert len(harness.rate_limit_peeks) == 1
    assert harness.rate_limit_records == []


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


async def test_get_available_number_returns_none_marks_lead_finished(
    harness, fake_redis
):
    """
    Permanent failure: ``_get_available_number`` returning None means the
    template's telephony_number_id is missing/disabled (or the fallback pool
    has nothing). The old behavior — defer 10s and retry forever — was a
    hot loop on an unresolvable state.

    Post-fix: mark the lead FINISHED with outcome NUMBER_UNAVAILABLE and
    fire a throttled P1 alert. No defer, no channel consumed.

    Capacity exhaustion is a *separate* path (channel-token gate); this
    test pins the misconfiguration branch.
    """
    lead = make_lead("lead-nonum")
    harness.add_lead(lead)
    harness.get_available_returns_none = True

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-nonum")
    await worker._iteration(session=None)

    # No dial, no defer-retry, channel pool untouched.
    assert harness.call_recorder.calls == []
    assert harness.deferred == []
    assert await channel_tokens_available(harness.number.id) == 1
    # Lead is finalized — exactly one completion write with the new outcome.
    assert len(harness.completions) == 1
    assert harness.completions[0]["outcome"] == "NUMBER_UNAVAILABLE"
    assert harness.completions[0]["status"] == LeadCallStatus.FINISHED
    assert lead.status == LeadCallStatus.FINISHED
    assert lead.outcome == "NUMBER_UNAVAILABLE"
    # Throttled alert fired once with the right scope.
    assert len(harness.no_telephony_number_alerts) == 1
    assert harness.no_telephony_number_alerts[0]["reseller_id"] == lead.reseller_id
    assert harness.no_telephony_number_alerts[0]["template"] == lead.template
    # Rate-limit ZSET untouched.
    assert harness.rate_limit_records == []


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


# ---------------------------------------------------------------------------
# Execution-mode gate (Daily / web-mode leads must not dial PSTN)
# ---------------------------------------------------------------------------


async def test_daily_lead_reaching_worker_is_dropped(harness, fake_redis):
    """
    Defensive backstop: if a DAILY-mode lead somehow gets on the ready list
    (e.g., a bypassed ingest path), the worker drops it BEFORE acquiring a
    channel token and BEFORE calling make_call. No phantom Plivo/Twilio dial.

    This is the test that pins the fix for the reported bug:
        "When tested with daily, this lead is also getting scheduled and a
         Plivo call is getting initiated."
    """
    from app.schemas import ExecutionMode

    lead = make_lead("lead-daily")
    lead.execution_mode = ExecutionMode.DAILY  # ← key difference
    harness.add_lead(lead)

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-daily-drop")
    await worker._iteration(session=None)

    # Worker did NOT call make_call — no phantom telephony dial.
    assert harness.call_recorder.calls == []
    # Channel token NEVER consumed.
    assert await channel_tokens_available(harness.number.id) == 1
    # Lead was NOT locked, NOT deferred, NOT released — worker exited before
    # acquire_lock_on_lead_by_id.
    assert lead.id not in harness.locked_lead_ids
    assert harness.deferred == []
    assert lead.id not in harness.released_locks
    # Status untouched.
    assert lead.status == LeadCallStatus.BACKLOG


async def test_reaper_does_not_reschedule_non_dispatchable_lead(
    harness, fake_redis, monkeypatch
):
    """
    Defence-in-depth for the rare crash window: if a worker dies between
    RPUSH-processing and LREM-processing while holding a DAILY lead (e.g.
    crashed during the is_dispatchable defensive check), the reaper must
    clean the tracking entry but NOT re-ZADD onto SCHEDULE_ZSET. Otherwise
    the lead would loop: promote → worker drops → idle until reaper next
    tick. The reaper mirrors the worker's defensive backstop.
    """
    from app.schemas import ExecutionMode

    lead = make_lead("lead-stuck-daily")
    lead.execution_mode = ExecutionMode.DAILY
    harness.add_lead(lead)
    monkeypatch.setattr(recon_mod, "get_lead_by_id", harness.get_lead_by_id)

    worker_uuid = "w-crashed-with-daily"
    proc_key = processing_list_for(worker_uuid)
    await fake_redis.client.rpush(proc_key, lead.id)
    # No heartbeat — worker is presumed dead.
    assert worker_heartbeat_key(worker_uuid) not in fake_redis.client.kv

    # Pre-condition: schedule is empty.
    assert await fake_redis.client.zcard(SCHEDULE_ZSET) == 0

    await recon_mod.reap_stuck_processing_lists()

    # Reaper must NOT re-schedule the Daily lead (no phantom dial loop).
    assert await fake_redis.client.zcard(SCHEDULE_ZSET) == 0
    # Tracking entry IS cleaned.
    assert (
        proc_key not in fake_redis.client.lists
        or fake_redis.client.lists[proc_key] == []
    )


async def test_reaper_still_reschedules_dispatchable_lead(
    harness, fake_redis, monkeypatch
):
    """
    Regression guard: the new is_dispatchable filter must NOT break the
    happy-path recovery for TELEPHONY leads. A crashed worker's TELEPHONY
    BACKLOG lead must still get re-ZADD'd onto SCHEDULE_ZSET.
    """
    lead = make_lead("lead-stuck-tel")  # default execution_mode = TELEPHONY
    harness.add_lead(lead)
    monkeypatch.setattr(recon_mod, "get_lead_by_id", harness.get_lead_by_id)

    worker_uuid = "w-crashed-with-tel"
    proc_key = processing_list_for(worker_uuid)
    await fake_redis.client.rpush(proc_key, lead.id)
    # No heartbeat.

    await recon_mod.reap_stuck_processing_lists()

    # TELEPHONY lead IS re-scheduled.
    assert await fake_redis.client.zcard(SCHEDULE_ZSET) == 1
    assert lead.id in fake_redis.client.zsets[SCHEDULE_ZSET]
    assert (
        proc_key not in fake_redis.client.lists
        or fake_redis.client.lists[proc_key] == []
    )

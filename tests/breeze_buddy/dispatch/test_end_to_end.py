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
    channel_semaphore as cs_mod,
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
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.core.config.static import BB_CHANNEL_WAIT_BACKOFF_MAX_S
from app.schemas import CallProvider, LeadCallStatus
from tests.breeze_buddy.dispatch.conftest import (
    AlwaysLeader,
    CallRecorder,
    make_lead,
    make_number,
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


async def test_post_cas_lost_keeps_the_line_for_the_live_call(
    harness, fake_redis, monkeypatch
):
    """
    CAS lost after make_call because the merchant aborted the lead during the
    dial: the call is LIVE, so the line belongs to it. The DB channel stays
    taken, the lead is stamped with the call id + number + marker (meta_data
    merged, not replaced), and the real handle_call_completion then returns
    the line exactly once WITHOUT touching the aborted lead.
    """
    from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
    from app.schemas.breeze_buddy.core import CALL_ATTACHED_AFTER_FINISH

    lead = make_lead("lead-cas")
    lead.metaData = {"aborted_at": "t0", "cancellation_reason": "merchant"}
    lead.outcome = "ABORT"
    harness.add_lead(lead)
    harness.cas_succeeds = False
    harness.cas_lost_status = LeadCallStatus.FINISHED  # merchant abort

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-cas")
    await worker._iteration(session=None)

    # Call WAS placed (CAS happens after make_call).
    assert len(harness.call_recorder.calls) == 1

    # The line stays with the live call: no DB channel given back, no token
    # returned, no re-defer; the lead keeps its terminal status and outcome.
    assert await channel_tokens_available(harness.number.id) == 0
    assert harness.released_numbers == []
    assert harness.deferred == []
    assert lead.status == LeadCallStatus.FINISHED
    assert lead.outcome == "ABORT"
    assert lead.id in harness.released_locks
    assert lead.call_id == "CA-test-sid"
    assert lead.telephony_number_id == harness.number.id
    assert lead.metaData["aborted_at"] == "t0"  # merged, not replaced
    assert lead.metaData[CALL_ATTACHED_AFTER_FINISH]["call_id"] == "CA-test-sid"

    # The call-end webhook, for real, with a NO_ANSWER outcome.
    released: list = []
    tokens: list = []
    completions: list = []
    retries: list = []

    async def _by_call(call_id):
        return lead if call_id == lead.call_id else None

    async def _noop(*a, **k):
        return None

    async def _get_number(number_id):
        return harness.number

    async def _release_number(number_id, provider):
        released.append(number_id)

    async def _release_token(number_id, token=None):
        tokens.append(number_id)
        return True

    async def _complete(**kw):
        completions.append(kw)
        return lead

    async def _retry(*a, **k):
        retries.append(a)

    async def _claim_release(lead_id):  # claim_attached_call_release_query's rule
        marker = (lead.metaData or {})[CALL_ATTACHED_AFTER_FINISH]
        if "released_at" in marker:
            return False
        marker["released_at"] = "now"
        return True

    async def _get():
        return fake_redis

    monkeypatch.setattr(calls_mod, "get_lead_by_call_id", _by_call)
    monkeypatch.setattr(calls_mod, "safe_release_pod", _noop)
    monkeypatch.setattr(calls_mod, "get_redis_service", _get)
    monkeypatch.setattr(calls_mod, "get_telephony_number_by_id", _get_number)
    monkeypatch.setattr(calls_mod, "_release_number", _release_number)
    monkeypatch.setattr(calls_mod, "release_channel_token", _release_token)
    monkeypatch.setattr(calls_mod, "_get_lead_config", harness._get_lead_config)
    monkeypatch.setattr(calls_mod, "update_lead_call_completion_details", _complete)
    monkeypatch.setattr(calls_mod, "_retry_call", _retry)
    monkeypatch.setattr(calls_mod, "claim_attached_call_release", _claim_release)

    await calls_mod.handle_call_completion("CA-test-sid", outcome="NO_ANSWER")
    await calls_mod.handle_call_completion("CA-test-sid", outcome="NO_ANSWER")  # dup

    assert released == [harness.number.id]  # the line comes back once
    assert tokens == [harness.number.id]
    assert completions == []  # ABORT outcome / meta_data not overwritten, no CRM mirror
    assert retries == []  # an aborted lead is not retried


async def test_post_cas_lost_on_a_db_error_releases_the_line(harness, fake_redis):
    """update_lead_call_details also returns None on a DB error, leaving the
    row BACKLOG. Stamping a call id on it and unlocking would let it be
    re-dialled, and the new dial would overwrite call_id and strand this
    call's line. The attach refuses a non-FINISHED row, so the worker falls
    back to releasing the line."""
    lead = make_lead("lead-cas-err")
    harness.add_lead(lead)
    harness.cas_succeeds = False  # lead stays BACKLOG

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    await w.Worker(worker_uuid="w-cas-err")._iteration(session=None)

    assert len(harness.call_recorder.calls) == 1
    assert lead.call_id is None  # nothing stamped on a BACKLOG row
    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.released_numbers == [harness.number.id]
    assert lead.id in harness.released_locks


def test_attach_query_only_stamps_a_finished_lead_without_a_call():
    """The SQL guard behind the two tests above, and a merge (not replace) of
    meta_data."""
    from datetime import datetime, timezone

    from app.database.queries.breeze_buddy.lead_call_tracker import (
        attach_placed_call_to_lead_query,
    )

    sql, values = attach_placed_call_to_lead_query(
        "lead-1", "CA-1", datetime.now(timezone.utc), "num-1"
    )
    assert "\"status\" = 'FINISHED'" in sql
    assert '"call_id" IS NULL' in sql
    assert "COALESCE(\"meta_data\", '{}')::jsonb ||" in sql
    assert '"outcome"' not in sql.split("SET")[1].split("WHERE")[0]
    assert "call_attached_after_finish" in values[4]


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


# ---------------------------------------------------------------------------
# Phantom-token hotfix — refused tokens are dropped, piles sleep longer
# ---------------------------------------------------------------------------


def _pin_pile_config(monkeypatch, threshold: int, long_defer: int) -> None:
    async def _threshold():
        return threshold

    async def _long():
        return long_defer

    monkeypatch.setattr(cs_mod.dyn_cfg, "BB_CAPACITY_WAIT_PILE_THRESHOLD", _threshold)
    monkeypatch.setattr(cs_mod.dyn_cfg, "BB_CAPACITY_WAIT_PILE_DEFER_S", _long)


async def test_refused_token_is_dropped_not_pushed_back(
    harness, fake_redis, monkeypatch
):
    """
    One phantom token in the list, Postgres says the number is full.

    The refusal consumes the token instead of pushing it back for the next
    worker to pop again; the lead is deferred and unlocked, nothing is dialled.
    """
    _pin_pile_config(monkeypatch, threshold=50, long_defer=60)
    lead = make_lead("lead-refused")
    harness.add_lead(lead)
    harness.acquire_number_succeeds = False

    await init_channel_semaphore(harness.number.id, 1)  # the phantom
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-refused")
    await worker._iteration(session=None)

    # The phantom is gone.
    assert await channel_tokens_available(harness.number.id) == 0
    # No dial, no DB number release (nothing was acquired).
    assert harness.call_recorder.calls == []
    assert harness.released_numbers == []
    # Lead handling unchanged: deferred, unlocked, still BACKLOG.
    assert len(harness.deferred) == 1
    assert harness.deferred[0][0] == "lead-refused"
    assert "lead-refused" not in harness.locked_lead_ids
    assert lead.status == LeadCallStatus.BACKLOG


async def test_pile_behind_full_number_costs_one_round_per_phantom(
    harness, fake_redis, monkeypatch
):
    """
    A full number with 17 phantom tokens and 2,000 leads queued behind it:
    17 refusals in total, an empty list afterwards, and the pile on the
    long defer once it passes the threshold.
    """
    _pin_pile_config(monkeypatch, threshold=50, long_defer=60)
    refusals: list[str] = []

    async def _counting_acquire(number):
        refusals.append(number.id)
        return w.NumberAcquire.FULL

    monkeypatch.setattr(w, "_acquire_number", _counting_acquire)

    n_leads, phantoms = 2000, 17
    for i in range(n_leads):
        lead = make_lead(f"lead-{i}")
        harness.add_lead(lead)
        await fake_redis.client.rpush(READY_LIST, lead.id)
    await init_channel_semaphore(harness.number.id, phantoms)

    worker = w.Worker(worker_uuid="w-pile-2000")
    for _ in range(n_leads):
        await worker._iteration(session=None)

    assert len(refusals) == phantoms
    assert await channel_tokens_available(harness.number.id) == 0
    assert harness.call_recorder.calls == []
    defers = [secs for _lead, secs in harness.deferred]
    assert len(defers) == n_leads
    assert all(1 <= s <= BB_CHANNEL_WAIT_BACKOFF_MAX_S for s in defers[:49])
    assert all(s == 60 for s in defers[49:])
    assert not harness.locked_lead_ids


async def test_pile_switches_defer_from_jitter_to_long(
    harness, fake_redis, monkeypatch
):
    """
    Number fully saturated (0 tokens). The first ``threshold - 1`` distinct
    leads defer with today's 1..MAX jitter; from the threshold on, every
    lead sleeps the long delay.
    """
    _pin_pile_config(monkeypatch, threshold=5, long_defer=60)
    await init_channel_semaphore(harness.number.id, 0)

    for i in range(8):
        lead = make_lead(f"lead-{i}")
        harness.add_lead(lead)
        await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-pile")
    for _ in range(8):
        await worker._iteration(session=None)

    defers = [secs for _lead, secs in harness.deferred]
    assert len(defers) == 8
    for secs in defers[:4]:
        assert 1 <= secs <= BB_CHANNEL_WAIT_BACKOFF_MAX_S
    assert defers[4:] == [60, 60, 60, 60]
    assert harness.call_recorder.calls == []


async def test_refused_path_also_uses_pile_defer(harness, fake_redis, monkeypatch):
    """The Postgres-refused defer (old fixed 5 s) follows the same rule."""
    _pin_pile_config(monkeypatch, threshold=1, long_defer=60)
    harness.acquire_number_succeeds = False
    lead = make_lead("lead-refused-pile")
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-refused-pile")
    await worker._iteration(session=None)

    assert harness.deferred == [("lead-refused-pile", 60)]


async def test_dial_path_unchanged_when_capacity_exists(
    harness, fake_redis, monkeypatch
):
    """With a free line the worker dials as before and no capwait key is
    written — the hotfix only touches the no-capacity branches."""
    _pin_pile_config(monkeypatch, threshold=50, long_defer=60)
    lead = make_lead("lead-ok")
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 2)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-ok")
    await worker._iteration(session=None)

    assert len(harness.call_recorder.calls) == 1
    assert harness.deferred == []
    assert await channel_tokens_available(harness.number.id) == 1
    assert not any(k.startswith("bb:capwait:") for k in fake_redis.client.hlls)


# ---------------------------------------------------------------------------
# #1258 follow-up — a real token is never thrown away
# ---------------------------------------------------------------------------


def _record_capacity_returns(monkeypatch, harness) -> list[str]:
    """Spy on the two halves of a pre-dial capacity return, in call order."""
    order: list[str] = []

    async def _release_number(number_id, provider):
        order.append("db")
        harness.released_numbers.append(number_id)

    async def _release_token(number_id, token=None):
        order.append("token")
        return await cs_mod.release_channel_token(number_id, token)

    monkeypatch.setattr(w, "_release_number", _release_number)
    monkeypatch.setattr(w, "release_channel_token", _release_token)
    return order


async def test_make_call_error_frees_db_line_before_token(
    harness, fake_redis, monkeypatch
):
    """
    make_call raises after the worker holds a token and the DB line.

    The LPUSH wakes a BLPOP'd worker at once; if it ran before our
    ``channels - 1`` it would be refused and drop the token, leaving a free
    line with nothing to dial on it until the reconciler. So the DB line is
    freed first, the token second — the call-end order.
    """
    order = _record_capacity_returns(monkeypatch, harness)
    harness.call_recorder = CallRecorder(raise_exc=RuntimeError("provider down"))
    lead = make_lead("lead-dial-error")
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-dial-error")
    await worker._iteration(session=None)

    assert order == ["db", "token"]
    assert harness.released_numbers == [harness.number.id]
    assert await channel_tokens_available(harness.number.id) == 1
    assert len(harness.deferred) == 1 and harness.deferred[0][0] == lead.id
    assert lead.status == LeadCallStatus.BACKLOG


async def test_no_sid_frees_db_line_before_token(harness, fake_redis, monkeypatch):
    """Same order on the no-SID reply path."""
    order = _record_capacity_returns(monkeypatch, harness)
    harness.call_recorder = CallRecorder(sid=None)
    lead = make_lead("lead-no-sid")
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-no-sid")
    await worker._iteration(session=None)

    assert order == ["db", "token"]
    assert harness.released_numbers == [harness.number.id]
    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.deferred == [(lead.id, 10)]


async def test_every_pre_dial_failure_path_returns_capacity_in_one_place():
    """
    The eight pre-dial failure paths all go through ``_return_capacity``;
    no path pairs the two releases by hand, so the order cannot drift.
    """
    import inspect

    src = inspect.getsource(w.Worker._dispatch)
    assert "release_channel_token(number.id, token)" in src  # the ERROR branch
    assert src.count("_return_capacity(number, token)") == 8
    assert "_release_number(" not in src


async def test_full_number_still_drops_the_token(harness, fake_redis, monkeypatch):
    """
    Exotel/Plivo/Vobiz: ``channels + 1 WHERE channels < max`` matched 0
    rows. The token matched no free line, so it is a phantom and is dropped
    (#1258), and the lead is deferred.
    """
    _pin_pile_config(monkeypatch, threshold=50, long_defer=60)
    harness.number = harness.number.model_copy(update={"provider": CallProvider.PLIVO})
    harness.acquire_number_outcome = w.NumberAcquire.FULL
    lead = make_lead("lead-full")
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-full")
    await worker._iteration(session=None)

    assert await channel_tokens_available(harness.number.id) == 0
    assert harness.call_recorder.calls == []
    assert harness.released_numbers == []
    assert len(harness.deferred) == 1 and harness.deferred[0][0] == lead.id
    assert lead.status == LeadCallStatus.BACKLOG


async def test_db_error_on_acquire_returns_the_token(harness, fake_redis, monkeypatch):
    """
    Postgres could not be asked. That says nothing about the line, so the
    token goes back (count returns to 1) and the lead is deferred, as the
    code did before #1258. Dropping it would idle a real line for up to a
    reconciler interval.
    """
    _pin_pile_config(monkeypatch, threshold=50, long_defer=60)
    harness.number = harness.number.model_copy(update={"provider": CallProvider.PLIVO})
    harness.acquire_number_outcome = w.NumberAcquire.ERROR
    lead = make_lead("lead-db-error")
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-db-error")
    await worker._iteration(session=None)

    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.call_recorder.calls == []
    assert harness.released_numbers == []  # nothing was acquired
    assert len(harness.deferred) == 1 and harness.deferred[0][0] == lead.id
    assert 1 <= harness.deferred[0][1] <= BB_CHANNEL_WAIT_BACKOFF_MAX_S
    assert lead.status == LeadCallStatus.BACKLOG


async def test_twilio_refusal_returns_the_token(harness, fake_redis, monkeypatch):
    """
    Twilio's status flip has no capacity clause: its only refusals are a DB
    error or a missing row, so a refused Twilio acquire never means "full"
    and the token is pushed back.
    """
    _pin_pile_config(monkeypatch, threshold=50, long_defer=60)
    assert harness.number.provider == CallProvider.TWILIO
    seen: list[str] = []

    async def _status_fails(number_id, status):
        seen.append(number_id)
        return None

    monkeypatch.setattr(calls_mod, "update_telephony_number_status", _status_fails)
    monkeypatch.setattr(w, "_acquire_number", calls_mod._acquire_number)
    lead = make_lead("lead-twilio-refused")
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-twilio-refused")
    await worker._iteration(session=None)

    assert seen == [harness.number.id]
    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.call_recorder.calls == []
    assert len(harness.deferred) == 1 and harness.deferred[0][0] == lead.id


# ---- the three-way acquire itself -----------------------------------------


def _plivo_number():
    return make_number("num-plivo").model_copy(update={"provider": CallProvider.PLIVO})


async def test_acquire_outcome_full_is_zero_rows(monkeypatch):
    async def _no_rows(number_id, raise_errors=False):
        return None

    monkeypatch.setattr(calls_mod, "increment_telephony_number_channels", _no_rows)
    assert await calls_mod._acquire_number(_plivo_number()) is (w.NumberAcquire.FULL)


async def test_acquire_outcome_error_is_a_raised_db_error(monkeypatch):
    async def _boom(number_id, raise_errors=False):
        assert raise_errors is True
        raise ConnectionError("pool exhausted")

    monkeypatch.setattr(calls_mod, "increment_telephony_number_channels", _boom)
    assert await calls_mod._acquire_number(_plivo_number()) is (w.NumberAcquire.ERROR)


async def test_acquire_outcome_acquired(monkeypatch):
    number = _plivo_number()

    async def _row(number_id, raise_errors=False):
        return number

    monkeypatch.setattr(calls_mod, "increment_telephony_number_channels", _row)
    assert await calls_mod._acquire_number(number) is (w.NumberAcquire.ACQUIRED)


async def test_acquire_outcome_twilio_never_full(monkeypatch):
    async def _none(number_id, status):
        return None

    monkeypatch.setattr(calls_mod, "update_telephony_number_status", _none)
    assert await calls_mod._acquire_number(make_number()) is (w.NumberAcquire.ERROR)


async def test_default_accessor_still_swallows_db_errors(monkeypatch):
    """
    Without ``raise_errors`` the accessor still folds a DB error into None
    (inbound admission reads that as "at capacity", as before), while the
    worker's acquire sees the error and answers ERROR.
    """
    from app.database.accessor.breeze_buddy import telephony_number as tn_mod

    async def _boom(query_text, values):
        raise ConnectionError("pool exhausted")

    monkeypatch.setattr(tn_mod, "run_parameterized_query", _boom)
    assert await tn_mod.increment_telephony_number_channels("num-x") is None
    assert await calls_mod._acquire_number(_plivo_number()) is (w.NumberAcquire.ERROR)

"""
Reconcilers (Plane 5) — heal Redis-vs-DB drift.

All four reconcilers are registered on the existing
``BackgroundTaskScheduler`` (``app/core/background_tasks/scheduler.py``).
That scheduler uses ``SET NX EX`` for distributed locking, so only one pod
runs each reconciler per interval. No new locking primitives needed.
"""

from __future__ import annotations

import time
from typing import Any, List, cast

from app.ai.voice.agents.breeze_buddy.dispatch.alerts import (
    clear_throttle,
    raise_channel_drift,
    raise_dispatch_halted,
    raise_no_leader,
    raise_schedule_depth_high,
)
from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    channel_exists,
    channel_tokens_available,
    init_channel_semaphore,
    topup_channel_tokens,
    trim_channel_tokens,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    PROCESSING_LIST_PREFIX,
    PROMOTER_LEADER,
    SCHEDULE_ZSET,
    worker_heartbeat_key,
)
from app.ai.voice.agents.breeze_buddy.dispatch.queue import (
    is_dispatchable,
    schedule_lead,
)
from app.core.config import dynamic as dyn_cfg
from app.core.logger import logger
from app.database.accessor import (
    get_all_outbound_numbers,
    get_lead_by_id,
)
from app.database.accessor.breeze_buddy.dispatch import (
    clean_stale_bb_locks as accessor_clean_stale_bb_locks,
)
from app.database.accessor.breeze_buddy.dispatch import (
    count_processing_by_outbound_number,
    get_unscheduled_backlog_leads,
)
from app.schemas import LeadCallStatus
from app.schemas.breeze_buddy.core import OutboundNumberStatus
from app.services.redis import get_redis_service

# ---------------------------------------------------------------------------
# reconcile_backlog_to_zset
# ---------------------------------------------------------------------------


async def reconcile_backlog_to_zset() -> None:
    """
    Find BACKLOG rows that should be on ``bb:schedule:leads`` but aren't,
    and ZADD them. Heals lost ingest events, Redis flushes, and
    worker-crash-before-RPUSH-processing windows.

    Bounded by ``BB_RECONCILE_BACKLOG_LIMIT`` (dynamic) per run; far-future
    leads are handled by subsequent ticks as their firing time approaches.
    """
    try:
        limit = await dyn_cfg.BB_RECONCILE_BACKLOG_LIMIT()
        leads = await get_unscheduled_backlog_leads(lookahead_seconds=120, limit=limit)
    except Exception as e:  # noqa: BLE001
        logger.error(f"reconcile_backlog_to_zset: DB read failed: {e}")
        return

    if not leads:
        return

    redis = await get_redis_service()
    client: Any = cast(Any, await redis.get_client())

    fixed = 0
    for lead_id, _reseller_id, score_ms in leads:
        try:
            existing = await client.zscore(SCHEDULE_ZSET, lead_id)
            if existing is None:
                await client.zadd(SCHEDULE_ZSET, {lead_id: score_ms})
                fixed += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"reconcile_backlog_to_zset: ZADD failed for {lead_id}: {e}")

    if fixed:
        logger.info(f"reconcile_backlog_to_zset: re-added {fixed} leads")


# ---------------------------------------------------------------------------
# reap_stuck_processing_lists
# ---------------------------------------------------------------------------


async def _scan_processing_lists(client: Any) -> List[str]:
    """Return processing-list keys via SCAN — safe on large keyspaces."""
    keys: List[str] = []
    cursor: int = 0
    while True:
        cursor, batch = await client.scan(
            cursor=cursor, match=f"{PROCESSING_LIST_PREFIX}*", count=100
        )
        keys.extend(batch)
        if cursor == 0:
            break
    return keys


async def reap_stuck_processing_lists() -> None:
    """
    For each processing list whose worker heartbeat has expired: read each
    lead's DB status, then either drop the tracking entry (call already
    in flight) or re-ZADD the lead (worker died before dialling).

    Note: per docs/BACKLOG_DISPATCHER_REDESIGN.md §7.1 and the
    conversation around Fix 2, this reaper deliberately does NOT clear
    ``is_locked``. That's left to ``clean_stale_bb_locks`` (10 min window)
    to preserve the safety margin around the §7.1 duplicate-call scenario.
    """
    redis = await get_redis_service()
    client: Any = cast(Any, await redis.get_client())
    try:
        keys = await _scan_processing_lists(client)
    except Exception as e:  # noqa: BLE001
        logger.error(f"reap_stuck_processing_lists: SCAN failed: {e}")
        return

    rescheduled = 0
    for proc_key in keys:
        worker_uuid = proc_key[len(PROCESSING_LIST_PREFIX) :]
        # If the worker is still alive (heartbeat key present), skip — its
        # in-flight leads are its responsibility.
        try:
            alive = await client.exists(worker_heartbeat_key(worker_uuid))
            if alive:
                continue
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"reap_stuck_processing_lists: heartbeat check failed "
                f"for {worker_uuid}: {e}"
            )
            continue

        try:
            lead_ids = await client.lrange(proc_key, 0, -1)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"reap_stuck_processing_lists: LRANGE failed: {e}")
            continue

        for lead_id in lead_ids:
            try:
                lead = await get_lead_by_id(lead_id)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"reap_stuck_processing_lists: DB read failed for {lead_id}: {e}"
                )
                continue

            if lead is None:
                # Lead no longer exists; drop the tracking entry.
                await client.lrem(proc_key, 1, lead_id)
                continue

            if lead.status == LeadCallStatus.PROCESSING:
                # Call was placed; nothing to do except clean tracking.
                await client.lrem(proc_key, 1, lead_id)
                continue

            if lead.status == LeadCallStatus.BACKLOG:
                # Worker died before dialling. Re-ZADD ONLY if the lead is
                # still dispatchable — a DAILY / HOLD_TRANSFER lead that
                # somehow ended up in a processing list (worker bug,
                # pre-existing bad data, manual ZADD) must NOT be
                # re-scheduled; that would create a one-cycle promote→drop
                # loop and waste worker iterations. Tracking is still
                # cleaned. Mirrors the worker's defensive backstop and the
                # reconciler-SQL execution_mode filter. We do NOT unlock
                # here (deliberate, see docstring).
                if lead.next_attempt_at is not None and is_dispatchable(
                    lead.execution_mode
                ):
                    await schedule_lead(lead_id, lead.next_attempt_at)
                    rescheduled += 1
                await client.lrem(proc_key, 1, lead_id)
                continue

            # FINISHED or other terminal state — drop tracking.
            await client.lrem(proc_key, 1, lead_id)

        # Empty list cleanup — best-effort.
        try:
            if (await client.llen(proc_key)) == 0:
                await client.delete(proc_key)
        except Exception:  # noqa: BLE001
            pass

    if rescheduled:
        logger.info(f"reap_stuck_processing_lists: re-scheduled {rescheduled} leads")


# ---------------------------------------------------------------------------
# reconcile_channel_tokens
# ---------------------------------------------------------------------------


async def reconcile_channel_tokens() -> None:
    """
    Heal channel-semaphore drift.

    For each active outbound number:
      - If the Redis LIST is missing, create it with M tokens (cold-start
        initialisation; this is the ONLY place channel state is created).
      - Otherwise compare ``LLEN`` against ``M - in_flight_calls_from_db``
        and top up or trim to match.
    """
    try:
        numbers = await get_all_outbound_numbers()
    except Exception as e:  # noqa: BLE001
        logger.error(f"reconcile_channel_tokens: DB read (numbers) failed: {e}")
        return

    if not numbers:
        return

    # Skip DISABLED numbers — they aren't dispatchable, so maintaining channel
    # state for them inflates Redis and skews drift alerts.
    numbers = [n for n in numbers if n.status != OutboundNumberStatus.DISABLED]
    if not numbers:
        return

    try:
        in_flight = await count_processing_by_outbound_number()
    except Exception as e:  # noqa: BLE001
        logger.error(f"reconcile_channel_tokens: DB count failed: {e}")
        return

    fixed_init = 0
    fixed_topup = 0
    fixed_trim = 0
    drift_alert_threshold = await dyn_cfg.BB_CHANNEL_DRIFT_ALERT_THRESHOLD()

    for n in numbers:
        # Preserve maximum_channels=0 (explicit zero capacity); only fall back
        # to 1 when the column is NULL.
        max_ch = n.maximum_channels if n.maximum_channels is not None else 1
        in_flight_n = in_flight.get(n.id, 0)
        expected_free = max(0, max_ch - in_flight_n)

        try:
            exists = await channel_exists(n.id)
        except Exception:  # noqa: BLE001
            continue

        if not exists:
            if await init_channel_semaphore(n.id, expected_free):
                fixed_init += 1
            continue

        try:
            actual = await channel_tokens_available(n.id)
        except Exception:  # noqa: BLE001
            continue

        drift = abs(actual - expected_free)
        if actual < expected_free:
            added = await topup_channel_tokens(n.id, expected_free - actual)
            if added:
                fixed_topup += 1
        elif actual > expected_free:
            removed = await trim_channel_tokens(n.id, actual - expected_free)
            if removed:
                fixed_trim += 1

        # Alert on sustained large drift — small drift (1–2 tokens) self-heals
        # silently every tick, but anything bigger usually means provider
        # webhooks are failing in volume.
        if drift > drift_alert_threshold:
            await raise_channel_drift(
                outbound_number_id=n.id,
                expected_free=expected_free,
                actual_free=actual,
            )

    if fixed_init or fixed_topup or fixed_trim:
        logger.info(
            f"reconcile_channel_tokens: init={fixed_init} "
            f"topup={fixed_topup} trim={fixed_trim}"
        )


# ---------------------------------------------------------------------------
# clean_stale_bb_locks
# ---------------------------------------------------------------------------


async def clean_stale_bb_locks() -> None:
    """
    Last-line defence: unlock rows that have been stuck with
    ``is_locked=TRUE`` for longer than the safety window. See
    docs/BACKLOG_DISPATCHER_REDESIGN.md §7.1 for the trade-off rationale
    (this window is what keeps the rare duplicate-call rate at 0–2/month).
    """
    try:
        threshold_minutes = await dyn_cfg.BB_STALE_LOCK_THRESHOLD_MINUTES()
        freed = await accessor_clean_stale_bb_locks(threshold_minutes=threshold_minutes)
    except Exception as e:  # noqa: BLE001
        logger.error(f"clean_stale_bb_locks failed: {e}")
        return

    if freed:
        logger.info(f"clean_stale_bb_locks: unlocked {len(freed)} stale rows")


# ---------------------------------------------------------------------------
# monitor_dispatch_health — alert-only reconciler (no state mutation).
# ---------------------------------------------------------------------------


async def monitor_dispatch_health() -> None:
    """
    Cheap periodic health check that raises Slack alerts for the three
    operational signals that need on-call attention:

      - **no leader**: ``bb:promoter:leader`` absent (dispatch is halted).
      - **dispatch halted**: leader present, but overdue leads piling up.
      - **schedule depth high**: ingest sustainably outpacing dispatch.

    All three are throttled by the alerts module so persistent conditions
    don't spam the channel. Healthy ticks clear the throttle for the two
    halt-style alerts so a brief recovery + relapse pages immediately.
    """
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())

        leader = await redis.get(PROMOTER_LEADER)
        schedule_size = int(await client.zcard(SCHEDULE_ZSET))
        now_ms = int(time.time() * 1000)
        overdue_count = int(await client.zcount(SCHEDULE_ZSET, "-inf", str(now_ms)))

        overdue_threshold = await dyn_cfg.BB_SCHEDULE_OVERDUE_ALERT_THRESHOLD()
        depth_threshold = await dyn_cfg.BB_SCHEDULE_DEPTH_ALERT_THRESHOLD()

        # No leader → dispatch is halted full-stop.
        if leader is None:
            await raise_no_leader()
        else:
            await clear_throttle("no_leader")

        # Leader present but overdue work is piling up → dispatch is degraded.
        if leader is not None and overdue_count > overdue_threshold:
            await raise_dispatch_halted(
                overdue_count=overdue_count, schedule_size=schedule_size
            )
        else:
            await clear_throttle("dispatch_halted")

        # Ingest outpacing dispatch — capacity issue, not a halt.
        if schedule_size > depth_threshold:
            await raise_schedule_depth_high(
                schedule_size=schedule_size,
                threshold=depth_threshold,
            )

    except Exception as e:  # noqa: BLE001
        logger.warning(f"monitor_dispatch_health failed: {e}")

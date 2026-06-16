"""
Schedule queue (Plane 2) — ZADD/ZREM helpers for ``bb:schedule:leads``.

The ZSET holds every lead awaiting dispatch, keyed by ``next_attempt_at`` as
unix-ms. Ingest and retry both call ``schedule_lead``; cancellation calls
``cancel_scheduled_lead``. Best-effort by design — DB is authoritative,
``reconcile_backlog_to_zset`` heals any drops.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any, Optional, cast

from app.ai.voice.agents.breeze_buddy.dispatch.keys import SCHEDULE_ZSET
from app.core.config.static import BB_DISPATCH_QPS_JITTER_MS
from app.core.logger import logger
from app.schemas import ExecutionMode
from app.services.redis import get_redis_service

# Execution modes the event-driven dispatcher places outbound calls for.
# Mirrors the WHERE clause in ``get_unscheduled_backlog_leads_query`` so the
# ingest path, retry path, and dispatch-now path all match the reconciler.
# TELEPHONY_ALERT is an outbound PSTN notification and uses the same worker
# path as regular telephony. DAILY/DAILY_TEST/DAILY_STREAM are web-only —
# they're either customer-initiated (inbound) or handled by a separate
# room-creation + notification flow, NOT by the worker's ``provider.make_call``
# PSTN dial. HOLD_TRANSFER is a transient mid-call leg, not a standalone
# outbound to schedule.
DISPATCHABLE_EXECUTION_MODES = frozenset(
    {
        ExecutionMode.TELEPHONY,
        ExecutionMode.TELEPHONY_TEST,
        ExecutionMode.TELEPHONY_ALERT,
    }
)


def is_dispatchable(execution_mode: ExecutionMode) -> bool:
    """Return True if a lead with this execution_mode should be put on the
    dispatch schedule (and processed by a worker)."""
    return execution_mode in DISPATCHABLE_EXECUTION_MODES


def _to_unix_ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def _apply_jitter(score_ms: int, jitter_ms: Optional[int] = None) -> int:
    """
    Add uniform ±jitter to a score so identical scheduled times don't all fire
    in the same millisecond. ``jitter_ms=0`` disables jitter (used by the
    manual dispatch-now endpoint where operator intent is literally now).
    """
    j = BB_DISPATCH_QPS_JITTER_MS if jitter_ms is None else jitter_ms
    if j <= 0:
        return score_ms
    return score_ms + random.randint(-j, j)


async def schedule_lead(
    lead_id: str,
    next_attempt_at: datetime,
    jitter_ms: Optional[int] = None,
) -> bool:
    """
    ZADD a lead onto the schedule.

    Returns True if the ZADD succeeded; False if Redis was unreachable.
    Caller should NOT treat False as fatal — the lead row in the DB is
    authoritative and ``reconcile_backlog_to_zset`` will pick it up.

    Args:
        lead_id: lead_call_tracker row id
        next_attempt_at: when this lead should fire (timezone-aware)
        jitter_ms: override default jitter; pass 0 for "no jitter" (operator)
    """
    score = _apply_jitter(_to_unix_ms(next_attempt_at), jitter_ms)
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        await client.zadd(SCHEDULE_ZSET, {lead_id: score})
        return True
    except Exception as e:  # noqa: BLE001 — best-effort; reconciler heals
        logger.error(f"schedule_lead: ZADD failed for {lead_id} (score={score}): {e}")
        return False


async def cancel_scheduled_lead(lead_id: str) -> bool:
    """
    ZREM a lead from the schedule. Called by abort handlers so the promoter
    doesn't pull a zombie. Safe to call even if the lead isn't on the schedule
    (ZREM is a no-op).
    """
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        await client.zrem(SCHEDULE_ZSET, lead_id)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"cancel_scheduled_lead: ZREM failed for {lead_id}: {e}")
        return False


async def get_scheduled_score(lead_id: str) -> Optional[int]:
    """Return the lead's ZSCORE (unix-ms) or None if not present."""
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        score = await client.zscore(SCHEDULE_ZSET, lead_id)
        return int(score) if score is not None else None
    except Exception as e:  # noqa: BLE001
        logger.error(f"get_scheduled_score failed for {lead_id}: {e}")
        return None


async def get_schedule_size() -> int:
    """Return current ZCARD — used by metrics and alerts."""
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        return int(await client.zcard(SCHEDULE_ZSET))
    except Exception as e:  # noqa: BLE001
        logger.error(f"get_schedule_size failed: {e}")
        return 0

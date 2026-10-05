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

from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    SCHEDULE_ZSET,
    lead_tier_key,
)
from app.core.config import dynamic as dyn_cfg
from app.core.config.static import BB_DISPATCH_QPS_JITTER_MS
from app.core.logger import logger
from app.schemas import ExecutionMode
from app.services.redis import get_redis_service

# Execution modes the event-driven dispatcher places outbound calls for.
# Mirrors the WHERE clause in ``get_unscheduled_backlog_leads_query`` so the
# ingest path, retry path, and dispatch-now path all match the reconciler.
# DAILY/DAILY_TEST/DAILY_STREAM are web-only — they're either customer-
# initiated (inbound) or handled by a separate room-creation + notification
# flow, NOT by the worker's ``provider.make_call`` PSTN dial. HOLD_TRANSFER
# is a transient mid-call leg, not a standalone outbound to schedule.
DISPATCHABLE_EXECUTION_MODES = frozenset(
    {ExecutionMode.TELEPHONY, ExecutionMode.TELEPHONY_TEST}
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


# Long enough for a lead to be dialled after its first schedule (leads are
# dialled within a day), so defers keep the tier without every defer path
# having to know the merchant. A lead waiting longer falls back to normal.
_LEAD_TIER_TTL_S = 48 * 3600

TIER_HIGH = "high"
TIER_MEDIUM = "medium"


async def merchant_tier(merchant_id: Optional[str]) -> Optional[str]:
    """``high`` / ``medium`` for a merchant in a priority list, else None.
    Config errors read as None (normal), never as a crash on the dial path."""
    if not merchant_id:
        return None
    try:
        if merchant_id in await dyn_cfg.BB_PRIORITY_HIGH_MERCHANT_IDS():
            return TIER_HIGH
        if merchant_id in await dyn_cfg.BB_PRIORITY_MEDIUM_MERCHANT_IDS():
            return TIER_MEDIUM
    except Exception as e:  # noqa: BLE001
        logger.warning(f"merchant_tier: config unavailable, treating as normal: {e}")
    return None


async def schedule_lead(
    lead_id: str,
    next_attempt_at: datetime,
    jitter_ms: Optional[int] = None,
    merchant_id: Optional[str] = None,
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
        merchant_id: when given, refreshes the lead's tier hint so the
            promoter routes it to the high / medium ready list. Callers
            without the lead row (defers) omit it and the existing hint,
            if any, stays.
    """
    score = _apply_jitter(_to_unix_ms(next_attempt_at), jitter_ms)
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
    except Exception as e:  # noqa: BLE001 — best-effort; reconciler heals
        logger.error(f"schedule_lead: Redis unavailable for {lead_id}: {e}")
        return False

    # Hint before the ZADD: a lead that is already due can be promoted the
    # moment it is on the schedule, and must find its tier already there.
    if merchant_id is not None:
        tier = await merchant_tier(merchant_id)
        try:
            if tier is None:
                await client.delete(lead_tier_key(lead_id))
            else:
                await client.set(lead_tier_key(lead_id), tier, ex=_LEAD_TIER_TTL_S)
        except Exception as e:  # noqa: BLE001 — the lead still dispatches, as normal
            logger.warning(f"schedule_lead: tier hint write failed for {lead_id}: {e}")

    try:
        await client.zadd(SCHEDULE_ZSET, {lead_id: score})
    except Exception as e:  # noqa: BLE001 — best-effort; reconciler heals
        logger.error(f"schedule_lead: ZADD failed for {lead_id} (score={score}): {e}")
        return False
    return True


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

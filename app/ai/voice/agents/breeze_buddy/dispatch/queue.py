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
from typing import Any, Dict, Optional, Sequence, Tuple, cast

from app.ai.voice.agents.breeze_buddy.dispatch.keys import SCHEDULE_ZSET
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    keys as v2_keys,
    scripts as v2_scripts,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2.latch import epoch_lost, v2_seen
from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import V2_ACCOUNTED_MODES
from app.ai.voice.agents.breeze_buddy.dispatch.v2.scripts import (
    Enqueue,
    Rank,
    rank_from_priority,
)
from app.core.config.static import BB_DISPATCH_QPS_JITTER_MS, BB_V2_MATCH_CAP
from app.core.logger import logger
from app.database import accessor
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


async def _ensure_route(template_id: str) -> Any:
    # lazy: routes -> managers.calls -> dispatch (import cycle)
    from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import ensure_route

    return await ensure_route(template_id)


async def _invalidate_route(template_id: str) -> None:
    from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import invalidate_route

    await invalidate_route(template_id)


async def _route_number(template_id: str) -> Optional[str]:
    from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import route_number

    return await route_number(template_id)


async def _number_mode_or_none(number_id: str) -> Optional[str]:
    from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import (
        number_mode_or_none,
    )

    return await number_mode_or_none(number_id)


async def _handback_pending_or_none(number_id: str) -> Optional[bool]:
    from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import (
        handback_pending_or_none,
    )

    return await handback_pending_or_none(number_id)


async def _epoch_present_or_none() -> Optional[bool]:
    from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import (
        epoch_present_or_none,
    )

    return await epoch_present_or_none()


async def _lead_template_id(lead_id: str) -> Optional[str]:
    lead = await accessor.get_lead_by_id(lead_id)
    return lead.template_id if lead is not None else None


async def _lead_rank(lead_id: str) -> Optional[Rank]:
    """The rank kept on the lead's row; None when the row is missing or unreadable, so
    that a failed read never turns a lead into the default rank."""
    lead = await accessor.get_lead_by_id(lead_id)
    if lead is None:
        return None
    return rank_from_priority((lead.metaData or {}).get("priority"))


async def _schedule_v2(
    lead_id: str,
    next_attempt_at: datetime,
    template_id: str,
    only_if_absent: bool = False,
    rank: Optional[Rank] = None,
) -> Optional[bool]:
    """
    Put the lead in its template's v2 room. True/False is the final answer;
    None means "this number is not v2-accounted: use today's schedule".
    """
    # No jitter in v2 (design card rule 16): it only spread today's promoter batches.
    due_ms = _to_unix_ms(next_attempt_at)

    async def enqueue() -> Optional[int]:
        ranked: Dict[str, Any] = {} if rank is None else {"rank": rank}
        return await v2_scripts.enqueue(
            template_id, lead_id, due_ms, only_if_absent, **ranked
        )

    issued = await enqueue()
    if issued == Enqueue.ROUTE_MISSING:
        # The route is missing from Redis (first use, or a flush): resolve it, which
        # writes bb:route:{T}, and retry (rule 18). enqueue reads the route itself, so
        # a schedule with the route present costs no extra read.
        try:
            await _ensure_route(template_id)
        except Exception as e:  # noqa: BLE001 — DB errors must not escape
            logger.error(f"schedule_lead: ensure_route failed for {template_id}: {e}")
        issued = await enqueue()
        if issued == Enqueue.ROUTE_MISSING:
            # Still missing (that resolve failed): re-resolve once more, then give up.
            try:
                await _invalidate_route(template_id)
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"schedule_lead: invalidate_route failed for {template_id}: {e}"
                )
            issued = await enqueue()
    if issued == Enqueue.NEED_RANK:
        # a ranked number: the lead's rank comes from its row, read once
        rank = await _lead_rank(lead_id)
        if rank is not None:
            issued = await enqueue()
    return await _enqueued(issued, template_id)


async def _enqueued(issued: Optional[int], template_id: str) -> Optional[bool]:
    """What an enqueue's reply means after any route retries: True/False is the final
    answer; None means "use today's schedule"."""
    if issued is None:
        return False  # Redis failed; a ZADD is not safe for a v2 number. Backlog reconciler heals.
    if issued == Enqueue.HOLDS_LINE:
        return True  # its holder re-queues it (rule 17)
    if issued == Enqueue.NEED_RANK:
        return False  # rank unreadable: never today's schedule; the backlog job heals
    if issued < 0:
        return None  # NOT_V2, or ROUTE_MISSING after the retries: today's schedule
    if issued == BB_V2_MATCH_CAP:
        await _match_the_rest(template_id)
    return True


async def _match_the_rest(template_id: str) -> None:
    """The enqueue's own match stopped at the cap with lines still free (a window
    opening, a big push): fill them now, not 100 a second on the sweep's ticks. The
    lead is queued either way, so a failed read here only leaves the rest to the sweep.
    """
    try:
        number_id = await _route_number(template_id)
    except Exception as e:  # noqa: BLE001 — the sweep matches the number next tick
        logger.error(f"schedule_lead: route read failed for {template_id}: {e}")
        return
    if number_id:
        await v2_scripts.match_all(number_id)


async def schedule_lead(
    lead_id: str,
    next_attempt_at: datetime,
    jitter_ms: Optional[int] = None,
    template_id: Optional[str] = None,
    only_if_absent: bool = False,
    *,
    rank: Optional[Rank] = None,
) -> bool:
    """
    ZADD a lead onto the schedule (or, for a v2 number, into its template's room).

    Returns True if the lead was queued; False if Redis was unreachable.
    Caller should NOT treat False as fatal — the lead row in the DB is
    authoritative and ``reconcile_backlog_to_zset`` will pick it up.

    Args:
        lead_id: lead_call_tracker row id
        next_attempt_at: when this lead should fire (timezone-aware)
        jitter_ms: override default jitter; pass 0 for "no jitter" (operator)
        template_id: the lead's template; lets v2 find the lead's number
        rank: the lead's place on a ranked v2 number (default: read from its row)
    """
    if template_id and await v2_seen():
        queued = await _schedule_v2(
            lead_id, next_attempt_at, template_id, only_if_absent, rank
        )
        if queued is not None:
            return queued
    return await _schedule_today(lead_id, next_attempt_at, jitter_ms)


async def schedule_backlog_v2(leads: Sequence[Tuple[Any, ...]]) -> int:
    """``schedule_lead(lead_id, next_attempt_at, template_id=..., only_if_absent=True)``
    for each (lead_id, next_attempt_at, template_id[, rank]) of a backlog page, with their
    enqueues in one round trip: each reply means what it does for one lead. A lead whose
    route is missing goes through ``schedule_lead`` alone, which resolves it (rule 18).
    How many ``schedule_lead`` would have answered True for."""
    if not leads:
        return 0
    # No v2_seen() gate: the caller read these templates' numbers as v2-accounted, and
    # enqueue re-checks the mode itself (NOT_V2: today's schedule).
    replies = await v2_scripts.enqueue_many(
        [(row[2], row[0], _to_unix_ms(row[1]), *row[3:]) for row in leads],
        only_if_absent=True,
    )
    queued = 0
    for (lead_id, at, template_id, *rank), issued in zip(leads, replies, strict=True):
        if issued in (Enqueue.ROUTE_MISSING, Enqueue.NEED_RANK):
            ranked: Dict[str, Any] = {"rank": rank[0]} if rank else {}
            ok = await schedule_lead(
                lead_id, at, template_id=template_id, only_if_absent=True, **ranked
            )
        else:
            settled = await _enqueued(issued, template_id)
            ok = settled if settled is not None else await _schedule_today(lead_id, at)
        queued += ok
    return queued


async def _schedule_today(
    lead_id: str, next_attempt_at: datetime, jitter_ms: Optional[int] = None
) -> bool:
    """ZADD the lead onto today's schedule; False if Redis was unreachable."""
    score = _apply_jitter(_to_unix_ms(next_attempt_at), jitter_ms)
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        await client.zadd(SCHEDULE_ZSET, {lead_id: score})
        return True
    except Exception as e:  # noqa: BLE001 — best-effort; reconciler heals
        logger.error(f"schedule_lead: ZADD failed for {lead_id} (score={score}): {e}")
        return False


async def v2_owns_number(number_id: str) -> Optional[bool]:
    """
    Is the number v2-accounted (``v2_pending`` / ``v2`` / ``draining``), so today's
    worker must not dial on it (design card rule 21)? False while v2 has never been
    used (no read at all); None when its mode can't be read.

    Also None, for every number, while ``bb:epoch`` is missing after this process saw
    it set (``latch.epoch_lost``; never set yet is first use, no hold): Redis lost v2's
    state, maybe the v2 flags with it, so a number reads as legacy while today's
    counters for it may be stale (x7b). The worker defers until the sweep leader's
    recovery sets the epoch again. Read after the mode, so a loss between the two reads
    is still seen; an unreadable epoch defers too.
    """
    if not await v2_seen():
        return False
    mode = await _number_mode_or_none(number_id)
    if mode is None:
        return None
    if mode in V2_ACCOUNTED_MODES:
        return True
    if await _handback_pending_or_none(number_id):
        # a hand-back has not written today's counters yet: wait for it (an unread flag
        # does not hold a legacy number; the mode read above just succeeded)
        return None
    present = await _epoch_present_or_none()
    if present is None or epoch_lost(present):
        return None
    return False


async def requeue_in_room(
    lead_id: str, next_attempt_at: datetime, template_id: Optional[str]
) -> Optional[bool]:
    """
    Put a lead today's worker picked back in its template's v2 room. None = it can't go
    to a room: no template, or its route can't be resolved, or the route still points at
    a number v2 doesn't own after one re-resolve (the worker's own number rule says v2
    owns the number, so the stored route was stale: design card rule 2).
    """
    if not template_id:
        return None
    queued = await _schedule_v2(lead_id, next_attempt_at, template_id)
    if queued is None:
        try:
            await _invalidate_route(template_id)
        except Exception as e:  # noqa: BLE001 — DB errors must not escape
            logger.error(f"requeue_in_room: re-resolve failed for {template_id}: {e}")
        queued = await _schedule_v2(lead_id, next_attempt_at, template_id)
    return queued


async def cancel_scheduled_lead(
    lead_id: str, template_id: Optional[str] = None
) -> bool:
    """
    ZREM a lead from the schedule. Called by abort handlers so the promoter
    doesn't pull a zombie. Safe to call even if the lead isn't on the schedule
    (ZREM is a no-op). Once v2 is in use it also leaves its template's v2 room
    (``template_id`` is looked up when not given), so no v2 ticket is issued for it.
    """
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        await client.zrem(SCHEDULE_ZSET, lead_id)
    except Exception as e:  # noqa: BLE001
        logger.error(f"cancel_scheduled_lead: ZREM failed for {lead_id}: {e}")
        return False
    try:
        if await v2_seen():
            template_id = template_id or await _lead_template_id(lead_id)
            if template_id:
                await client.zrem(v2_keys.room_key(template_id), lead_id)
                await client.hdel(v2_keys.qp_key(template_id), lead_id)
    except Exception as e:  # noqa: BLE001 — a ticket for it gives the line back
        logger.error(f"cancel_scheduled_lead: v2 room ZREM failed for {lead_id}: {e}")
    return True


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

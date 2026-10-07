"""Small v2 health checks (design card §5), alerting through ``dispatch/alerts.py``.

``run_monitors`` runs on the sweep leader every 15 s:

- a ticket waiting > 10 s for an acceptor (dialler pods down, or all at their in-flight
  guard);
- a number in mode ``v2`` whose ``bb:due`` time passed > 5 s ago, on two checks in a row:
  every match rewrites its number's entry to now or later, so nothing has matched it;
- a line waiting > 5 s in ``bb:grants`` for its lead row (the grant worker is down or
  slow).

None while the kill switch is off: it holds tickets and matches on purpose.

``check_sweep_leader`` runs on every pod's sweeper, since a missing leader can't report
itself: no leader for 10 s alerts.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional, Set, Tuple

from app.ai.voice.agents.breeze_buddy.dispatch.alerts import (
    raise_v2_grants_waiting,
    raise_v2_idle_with_due_lead,
    raise_v2_no_sweep_leader,
    raise_v2_tickets_waiting,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import keys as k, scripts
from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import _client
from app.ai.voice.agents.breeze_buddy.dispatch.v2.scripts import (
    Ticket,
    parse_grant,
    parse_ticket,
)
from app.core.config.static import (
    BB_V2_DUE_BATCH,
    BB_V2_MONITOR_SCAN_CHUNK,
    BB_V2_MONITOR_SCAN_MAX,
)
from app.core.logger import logger

TICKET_WAIT_ALERT_MS = 10_000
GRANT_WAIT_ALERT_MS = 5_000  # a line waiting for its lead row (bb:grants)
DUE_GRACE_MS = 5_000
NO_LEADER_ALERT_S = 10.0

# v2 numbers left unmatched past their due time at the last check (alert on two in a row).
_idle_last: Set[str] = set()
# When this pod first saw no sweep leader (monotonic seconds); None = a leader exists.
_leader_missing_since: Optional[float] = None


# bb:tickets is read from the head in chunks until the first live ticket no coroutine has
# claimed. Void entries (reaped, re-issued or handed-back tickets, and a re-pushed ticket's
# claimed twin: claim refuses them all) are skipped. At most BB_V2_MONITOR_SCAN_MAX
# entries: acceptors pop void entries as fast as live ones, so a void run that long at the
# head means none is popping, and the head's age is how long everything behind it waited.


async def _oldest_waiting_ticket(c: Any, now_ms: int) -> Tuple[Optional[str], int]:
    """(number, age in ms) of the oldest live ticket no coroutine has claimed: the list
    is in issue order, so it is the first live entry from the head. Two round trips per
    chunk; one chunk when the list is healthy (near empty)."""
    head: Optional[Ticket] = None
    for start in range(0, BB_V2_MONITOR_SCAN_MAX, BB_V2_MONITOR_SCAN_CHUNK):
        end = start + BB_V2_MONITOR_SCAN_CHUNK - 1
        raws = await c.lrange(k.TICKETS_KEY, start, end)
        entries = [t for t in map(parse_ticket, raws) if t is not None]
        if entries:
            head = head or entries[0]
            async with c.pipeline(transaction=False) as pipe:
                for ticket in entries:
                    pipe.hget(k.inflight_key(ticket.number_id), ticket.lead_id)
                leases = await pipe.execute()
            for ticket, raw in zip(entries, leases):
                lease = json.loads(raw) if raw else {}
                if lease.get("tk") == ticket.tk and "owner" not in lease:
                    return ticket.number_id, now_ms - ticket.issued_ms
        if len(raws) < BB_V2_MONITOR_SCAN_CHUNK:
            return None, 0  # the whole list read: no live ticket waits
    if head is None:
        return None, 0
    return head.number_id, now_ms - head.issued_ms


async def _unmatched_due_numbers(c: Any, now_ms: int) -> Set[str]:
    """v2 numbers whose ``bb:due`` time passed more than DUE_GRACE_MS ago (at most
    BB_V2_DUE_BATCH, the earliest). A v2_pending or draining number keeps its entry
    unmatched on purpose. Two round trips."""
    late = await c.zrangebyscore(
        k.DUE_KEY, "-inf", now_ms - DUE_GRACE_MS, start=0, num=BB_V2_DUE_BATCH
    )
    if not late:
        return set()
    async with c.pipeline(transaction=False) as pipe:
        for number_id in late:
            pipe.hget(k.num_key(number_id), "mode")
        modes = await pipe.execute()
    return {n for n, mode in zip(late, modes) if mode == "v2"}


RANK_BAND = 10**13  # scripts.py: a ready lead's score is (rank - 100) * RANK_BAND + t


async def _log_ranks(c: Any, now_ms: int) -> None:
    """One line per waiting room of a ranked number: the leads ready in each rank, those
    waiting for a later time, and how long the oldest ready rank-1 lead has waited (read
    as "first ready first", which is what rank 1 is; None when there is none)."""
    for n in sorted(await c.smembers(k.V2_ACTIVE_KEY)):
        ranked, live_day = await c.hmget(k.num_key(n), "ranked", "live_day")
        if ranked != "1":
            continue
        for t in sorted(await c.smembers(k.numtpl_key(n))):
            room, ready, wait_ms, floor = k.room_key(t), {}, None, "-inf"
            # the head of each rank in use, lowest first, then that rank's count
            while head := await c.zrangebyscore(
                room, floor, "(0", start=0, num=1, withscores=True
            ):
                rank = int(head[0][1] // RANK_BAND) + 100
                if rank == 1:
                    at = head[0][1] + 99 * RANK_BAND  # the t of scripts.py's pscore
                    if live_day == "1":
                        at = (100000 - at // 10**8) * 86_400_000 + at % 10**8
                        at -= scripts.IST_OFFSET_S * 1000
                    wait_ms = int(now_ms - at)
                floor = (rank - 99) * RANK_BAND
                ready[rank] = await c.zcount(room, head[0][1], f"({floor}")
            later = await c.zcount(room, 0, "+inf")
            if ready or later:
                logger.info(
                    f"v2 ranks: number={n} template={t} ready={ready} later={later} "
                    f"oldest_rank_1_wait_ms={wait_ms}"
                )


async def run_monitors() -> None:
    global _idle_last
    c = await _client()
    if await c.get(k.ENABLED_MIRROR_KEY) == "0":
        # the kill switch holds tickets (the acceptors push them back) and matches
        _idle_last = set()
        return
    now_ms = int(time.time() * 1000)
    number_id, age_ms = await _oldest_waiting_ticket(c, now_ms)
    if number_id is not None and age_ms > TICKET_WAIT_ALERT_MS:
        logger.warning(f"v2 monitor: a ticket on {number_id} waiting {age_ms} ms")
        await raise_v2_tickets_waiting(number_id, age_ms // 1000)
    grant = parse_grant(await c.lindex(k.GRANTS_KEY, 0))  # the head is the oldest
    if grant is not None:
        age_ms, waiting = now_ms - grant[0].issued_ms, await c.llen(k.GRANTS_KEY)
        logger.info(f"v2 monitor: grants waiting={waiting} oldest_ms={age_ms}")
        if age_ms > GRANT_WAIT_ALERT_MS:
            await raise_v2_grants_waiting(grant[0].number_id, age_ms // 1000, waiting)
    idle = await _unmatched_due_numbers(c, now_ms)
    for number_id in sorted(idle & _idle_last):
        logger.warning(f"v2 monitor: {number_id} not matched since its bb:due time")
        await raise_v2_idle_with_due_lead(number_id)
    _idle_last = idle
    await _log_ranks(c, now_ms)


async def check_sweep_leader() -> None:
    global _leader_missing_since
    c = await _client()
    if await c.exists(k.SWEEP_LEADER_KEY):
        _leader_missing_since = None
        return
    now = time.monotonic()
    if _leader_missing_since is None:
        _leader_missing_since = now
    elif now - _leader_missing_since >= NO_LEADER_ALERT_S:
        logger.error("v2 monitor: no sweep leader")
        await raise_v2_no_sweep_leader()

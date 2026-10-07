"""v2 safety nets (design card §5, rules 11, 14, 19; Fable M2, M4, M6).

Run by the sweep leader on tick counters (``sweep.py``), each as its own job:

- ``reconcile_backlog_v2``: due BACKLOG rows of v2-accounted numbers that are in no room
  and hold no line go back to their room (``schedule_lead``, whose Lua does the checks).
- ``ledger_check``: busy lists only shrink here. A holder whose lead is FINISHED, or BACKLOG
  with no lease and no lock, is removed atomically; a live call missing from its busy list
  is alerted on, never added.
- ``reap_leases``: tickets nobody claimed, claimed but never dialled, or stuck dialling.
  Every change is ticket-checked in Lua.
- ``prune_orphans``: rooms only make sense on a v2-accounted number; anything else goes
  back to today's schedule.
- ``locked_leads_by_number`` / ``seed_holders``: what ``switch.py`` needs to switch a
  number on safely.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple, cast

from app.ai.voice.agents.breeze_buddy.dispatch.alerts import (
    raise_v2_grants_waiting,
    raise_v2_ledger_missing,
    raise_v2_mass_free_stopped,
)
from app.ai.voice.agents.breeze_buddy.dispatch.queue import (
    schedule_backlog_v2,
    schedule_lead,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import keys as k, scripts
from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import (
    _client,
    ensure_route,
    invalidate_route,
    number_modes,
    template_is_v2_accounted as _is_v2_template,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2.scripts import parse_ticket
from app.core.config.dynamic import BB_V2_BREAKER_SHARE
from app.core.config.static import (
    BB_V2_CLAIMED_MAX_AGE_S,
    BB_V2_DIAL_STUCK_S,
    BB_V2_GRANT_MAX_S,
    BB_V2_GRANT_RESEND_S,
    BB_V2_LEDGER_CHUNK,
    BB_V2_PRUNE_CHUNK,
    BB_V2_UNCLAIMED_REPUSH_S,
)
from app.core.logger import logger
from app.crm.outreach.contracts import (
    ranks_for_leads,
)
from app.database.accessor import release_lock_on_lead_by_id
from app.database.accessor.breeze_buddy.dispatch import (
    LeadDispatchState,
    get_due_backlog_page,
    get_finished_inbound_calls,
    get_known_inbound_calls,
    get_lead_dispatch_states,
    get_legacy_inflight_leads,
    get_live_calls_on_number,
    get_live_calls_on_numbers,
)
from app.schemas import LeadCallStatus

# Lease reaper tiers (design card rule 14; static.py checks their order against the
# timers they must outlast).
UNCLAIMED_REPUSH_MS = BB_V2_UNCLAIMED_REPUSH_S * 1000
LEASE_MAX_AGE_MS = BB_V2_CLAIMED_MAX_AGE_S * 1000
DIAL_STUCK_MS = BB_V2_DIAL_STUCK_S * 1000
GRANT_RESEND_MS = BB_V2_GRANT_RESEND_S * 1000
GRANT_MAX_MS = BB_V2_GRANT_MAX_S * 1000

BACKLOG = LeadCallStatus.BACKLOG.value
PROCESSING = LeadCallStatus.PROCESSING.value
FINISHED = LeadCallStatus.FINISHED.value


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ms(when: Optional[datetime]) -> int:
    return int(when.timestamp() * 1000) if when is not None else 0


# ---------------------------------------------------------------------------
# Backlog reconciler
# ---------------------------------------------------------------------------

# Page position, kept between runs so a big healthy pile can't hide a lost lead behind it
# (PoC issue 6). None = start from the oldest row.
_backlog_after: Optional[Tuple[datetime, str]] = None


async def reconcile_backlog_v2(page_size: int = 1000, max_pages: int = 5) -> int:
    """Re-queue due BACKLOG leads of v2-accounted numbers. Returns how many were queued."""
    global _backlog_after
    queued = 0
    is_v2: Dict[str, Optional[bool]] = {}
    for _ in range(max_pages):
        page = await get_due_backlog_page(_backlog_after, page_size)
        leads: List[Tuple[Any, ...]] = []
        for lead_id, template_id, next_attempt_at, *priority in page:
            if not template_id:
                continue
            if template_id not in is_v2:
                is_v2[template_id] = await _is_v2_template(template_id)
            if is_v2[template_id] is True:  # else today's number, or unreadable: skip
                # with the row's rank (none = the number's default), read with the page
                rank = map(scripts.rank_from_priority, priority)
                leads.append((lead_id, next_attempt_at, template_id, *rank))
        # One round trip per page. enqueue's Lua skips a lead that holds a line, and
        # (only_if_absent) one already in its room: a big waiting pile costs a ZSCORE per
        # lead, not a write + match, and a stale page never moves a due time (Fable M2).
        queued += await schedule_backlog_v2(leads)
        if len(page) < page_size:
            _backlog_after = (
                None  # reached the end: the next run starts from the oldest
            )
            break
        _backlog_after = (page[-1][2], page[-1][0])
    if queued:
        logger.info(f"v2 backlog reconciler queued {queued} leads")
    return queued


# ---------------------------------------------------------------------------
# Ledger check
# ---------------------------------------------------------------------------

# PROCESSING leads missing from each number's busy list at the last check (rule 19).
_missing_last: Dict[str, Set[str]] = {}
# ``call:`` holders with no lead row at the last check, per number.
_rowless_last: Dict[str, Set[str]] = {}


def _stale(state: Optional[LeadDispatchState]) -> bool:
    """The holder can't own a line: its lead ended, or it waits with no ticket and nobody
    dispatching it. A locked BACKLOG lead is alive (a dispatch is running)."""
    if state is None:
        return False
    if state.status == FINISHED:
        return True
    return state.status == BACKLOG and not state.is_locked


def _chunks(ids: List[str]) -> List[List[str]]:
    return [
        ids[i : i + BB_V2_LEDGER_CHUNK] for i in range(0, len(ids), BB_V2_LEDGER_CHUNK)
    ]


async def _lead_states(lead_ids: List[str]) -> Dict[str, LeadDispatchState]:
    """One query per chunk. A chunk the DB fails to answer is left out, and a lead with
    no state is never stale, so it keeps its line until a later run reads it: one DB
    error must not stop the run."""
    states: Dict[str, LeadDispatchState] = {}
    for chunk in _chunks(lead_ids):
        try:
            states.update(await get_lead_dispatch_states(chunk))
        except Exception as e:  # noqa: BLE001 — see the docstring
            logger.error(f"v2 ledger: lead states unread for {len(chunk)} leads: {e}")
    return states


async def _free_stale_leads(c: Any, candidates: List[Tuple[str, str]]) -> int:
    """Free the (number, lead) holders the batched read found stale. That read can be
    seconds old by now, time enough for a lead to be ticketed, dialled and its lease
    cleared again, its holder then a live call's line. So the candidates'
    leases and then their states are read again, in rule 11's order, and each holder is
    freed right after."""
    if not candidates:
        return 0
    async with c.pipeline(transaction=False) as pipe:
        for number_id, lead_id in candidates:
            pipe.hexists(k.inflight_key(number_id), lead_id)
        leased = await pipe.execute()
    unleased = [cand for cand, has in zip(candidates, leased) if not has]
    states = await _lead_states(sorted({lead_id for _, lead_id in unleased}))
    freed = 0
    for number_id, lead_id in unleased:
        state = states.get(lead_id)
        if not _stale(state):
            continue
        try:
            freed += await _free_stale_lead(
                number_id, lead_id, cast(LeadDispatchState, state)
            )
        except Exception as e:  # noqa: BLE001 — one holder must not stop the rest
            logger.error(
                f"v2 ledger: freeing lead:{lead_id} on {number_id} failed: {e}"
            )
    return freed


async def _free_stale_lead(
    number_id: str, lead_id: str, state: LeadDispatchState
) -> int:
    # atomic: removed only if still no lease (a ticket may have landed meanwhile)
    if await scripts.release_stale(number_id, lead_id) != 1:
        return 0
    if state.status == BACKLOG and state.next_attempt_at is not None:
        # back to its room at once, not after the backlog reconciler (Fable M6)
        ranked: Dict[str, Any] = (
            {"rank": scripts.rank_from_priority(state.priority)}
            if state.priority
            else {}
        )
        await schedule_lead(
            lead_id, state.next_attempt_at, template_id=state.template_id, **ranked
        )
    return 1


async def _free_ended_calls(
    number_id: str, call_ids: List[str], ended: Set[str], known: Set[str]
) -> int:
    """Free ``call:`` holders whose inbound call ended, and those with no lead row on two
    checks in a row (the answer path admits a moment before it inserts the row, so one
    sighting is normal; two mean the insert never happened)."""
    rowless = set(call_ids) - known
    repeat = rowless & _rowless_last.get(number_id, set())
    _rowless_last[number_id] = rowless - repeat
    freed = 0
    for call_id in sorted((set(call_ids) & ended) | repeat):
        reply = await scripts.release(number_id, f"call:{call_id}")
        freed += 1 if reply is not None and reply[0] == 1 else 0
    return freed


async def _alert_missing(
    c: Any, number_ids: List[str], modes: Dict[str, Optional[str]]
) -> None:
    """Alert on outbound calls missing from a ``v2`` number's busy list two checks in a
    row; a line is freed a moment before its row turns FINISHED, so one sighting is
    normal. Never adds: an added holder for a call that just ended would leak a line.
    The DB is read before the busy lists, as before (one query per chunk of numbers)."""
    v2_numbers = [number_id for number_id in number_ids if modes.get(number_id) == "v2"]
    for number_id in number_ids:
        if modes.get(number_id) != "v2":  # legacy calls are seeded only at v2's start
            _missing_last.pop(number_id, None)
    if not v2_numbers:
        return
    live: Dict[str, List[Tuple[str, str, Optional[str]]]] = {}
    checked: List[str] = []
    for chunk in _chunks(v2_numbers):
        try:
            live.update(await get_live_calls_on_numbers(chunk))
            checked += chunk
        except Exception as e:  # noqa: BLE001 — unread numbers wait for the next run
            logger.error(f"v2 ledger: live calls unread for {len(chunk)} numbers: {e}")
    if not checked:
        return
    async with c.pipeline(transaction=False) as pipe:
        for number_id in checked:
            pipe.smembers(k.busy_key(number_id))
        busy_now = await pipe.execute()
    for number_id, busy in zip(checked, busy_now):
        missing = {
            lead_id
            for lead_id, direction, _ in live.get(number_id, [])
            if direction == "OUTBOUND" and f"lead:{lead_id}" not in busy
        }
        repeat = missing & _missing_last.get(number_id, set())
        _missing_last[number_id] = missing
        if repeat:
            logger.error(
                f"v2 ledger: live calls missing from bb:busy:{number_id}: {repeat}"
            )
            await raise_v2_ledger_missing(number_id, sorted(repeat))


BREAKER_FLOOR = 50  # a run that would free no more lines than this is never stopped


async def _breaker_stops(job: str, would_free: int, held: int) -> bool:
    """The mass-free breaker (BB_V2_BREAKER_SHARE, 0 = off): a run that would free more
    than that share of all lines held, and more than BREAKER_FLOOR, frees nothing. So
    many at once is more likely a bad read than that many dead calls."""
    if would_free <= BREAKER_FLOOR:
        return False
    share = await BB_V2_BREAKER_SHARE()
    if not share or would_free <= held * share:
        return False
    logger.error(f"v2 {job}: would free {would_free} of {held} lines held; freed none")
    await raise_v2_mass_free_stopped(job, would_free, held)
    return True


async def ledger_check() -> dict:
    """Remove stale holders from the busy list of every v2-accounted number, in a fixed
    number of round trips and DB queries per BB_V2_LEDGER_CHUNK ids, whatever the number
    count (spec 2026-10-05 §4.10)."""
    c = await _client()
    number_ids = sorted(await c.smembers(k.V2_ACTIVE_KEY))
    if not number_ids:
        return {"removed": 0}
    modes = await number_modes(number_ids)  # one round trip (Fable M5)
    # Order matters (PoC bug, rule 11): holders, then leases, then statuses. Statuses
    # first can see BACKLOG just before PROCESSING is written and "no lease" just after
    # the coroutine clears it, and free a live call's line.
    async with c.pipeline(transaction=False) as pipe:
        for number_id in number_ids:
            pipe.smembers(k.busy_key(number_id))
        for number_id in number_ids:
            pipe.hkeys(k.inflight_key(number_id))
        replies = await pipe.execute()
    holders = dict(zip(number_ids, replies[: len(number_ids)]))
    leases = {
        number_id: set(v)
        for number_id, v in zip(number_ids, replies[len(number_ids) :])
    }
    lead_ids = {
        number_id: sorted(h[5:] for h in holders[number_id] if h.startswith("lead:"))
        for number_id in number_ids
    }
    call_ids = {
        number_id: sorted(h[5:] for h in holders[number_id] if h.startswith("call:"))
        for number_id in number_ids
    }
    states = await _lead_states(
        sorted({lead for leads in lead_ids.values() for lead in leads})
    )
    ended: Set[str] = set()
    known: Set[str] = set()
    # neither ended nor rowless: an unread chunk is not "no row"
    unread: Set[str] = set()
    for chunk in _chunks(
        sorted({call for calls in call_ids.values() for call in calls})
    ):
        try:
            chunk_ended = await get_finished_inbound_calls(chunk)
            known |= await get_known_inbound_calls(chunk)
            ended |= chunk_ended
        except Exception as e:  # noqa: BLE001 — those holders wait for the next run
            unread.update(chunk)
            logger.error(
                f"v2 ledger: inbound calls unread for {len(chunk)} holders: {e}"
            )
    candidates = [
        (number_id, lead_id)
        for number_id in number_ids
        for lead_id in lead_ids[number_id]
        if lead_id not in leases[number_id] and _stale(states.get(lead_id))
    ]
    calls = sum(
        call in ended or call not in known
        for number_id in number_ids
        for call in call_ids[number_id]
        if call not in unread
    )
    held = sum(len(h) for h in holders.values())
    if await _breaker_stops("ledger", len(candidates) + calls, held):
        await _alert_missing(c, number_ids, modes)
        return {"removed": 0}
    removed = await _free_stale_leads(c, candidates)
    for number_id in number_ids:
        try:
            read = [call for call in call_ids[number_id] if call not in unread]
            removed += await _free_ended_calls(number_id, read, ended, known)
        except Exception as e:  # noqa: BLE001 — one number must not stop the rest
            logger.error(f"v2 ledger check failed for {number_id}: {e}")
    await _alert_missing(c, number_ids, modes)
    if removed:
        logger.info(f"v2 ledger check freed {removed} stale lines")
    return {"removed": removed}


# ---------------------------------------------------------------------------
# Lease reaper
# ---------------------------------------------------------------------------


_Lease = Tuple[str, str, dict]  # (number, lead, the lease's JSON)


class _OldLeases(NamedTuple):
    unclaimed: List[_Lease]  # popped, never claimed (a pod died, a pop reply was lost)
    claimed: List[_Lease]  # claimed, never marked dialling
    stuck: List[_Lease]  # marked dialling, never cleared
    granted: List[_Lease]  # waiting for its lead row (no ticket yet): scripts.regrant


def _tier(lease: dict, now_ms: int, max_age_ms: int, dial_stuck_ms: int) -> str:
    """Which tier a lease has outgrown ("" = none yet). Raises KeyError / ValueError on a
    lease that lacks its fields."""
    if lease.get("g"):
        return "granted" if now_ms - int(lease["issued_ms"]) > GRANT_RESEND_MS else ""
    if "dialling_ms" in lease:
        stuck = now_ms - int(lease["dialling_ms"]) > dial_stuck_ms
        return "stuck" if stuck else ""
    if "owner" in lease:
        # a lease the previous release's take stamped has no claimed_ms: its issue
        since = int(lease.get("claimed_ms") or lease["issued_ms"])
        return "claimed" if now_ms - since > max_age_ms else ""
    since = int(lease.get("repushed_ms") or lease["issued_ms"])
    return "unclaimed" if now_ms - since > UNCLAIMED_REPUSH_MS else ""


async def _old_leases(
    c: Any, now_ms: int, max_age_ms: int, dial_stuck_ms: int
) -> _OldLeases:
    """Every lease past its tier: unclaimed for ``UNCLAIMED_REPUSH_MS`` since it was
    issued (or last re-pushed), claimed for ``max_age_ms`` without dialling, dialling for
    ``dial_stuck_ms``. A number has at most as many leases as lines."""
    old = _OldLeases([], [], [], [])
    numbers = sorted(await c.smembers(k.V2_ACTIVE_KEY))
    async with c.pipeline(transaction=False) as pipe:
        for number_id in numbers:
            pipe.hgetall(k.inflight_key(number_id))
        every = await pipe.execute()
    for number_id, leases in zip(numbers, every):
        for lead_id, raw in leases.items():
            try:
                lease = json.loads(raw)
                tier = _tier(lease, now_ms, max_age_ms, dial_stuck_ms)
            except (KeyError, ValueError):
                logger.error(f"v2 reaper: unreadable lease {number_id}/{lead_id}")
                continue
            if tier:
                getattr(old, tier).append((number_id, lead_id, lease))
    return old


async def _repush_unclaimed(c: Any, unclaimed: List[_Lease]) -> int:
    """Deliver popped-but-unclaimed tickets again; their lines stay held. The head of
    bb:tickets is read once, before any re-push: every ticket issued before it has left
    the list (rule 52). Newest first, so each goes back ahead of the previous one: the
    list stays in issue order."""
    if not unclaimed:
        return 0
    raw = await c.lindex(k.TICKETS_KEY, 0)
    head = parse_ticket(raw)
    if raw is not None and head is None:
        logger.error(f"v2 reaper: unreadable head of bb:tickets {raw!r}; no re-push")
        return 0
    repushed = 0
    newest_first = sorted(unclaimed, key=lambda x: int(x[2]["issued_ms"]), reverse=True)
    for number_id, lead_id, lease in newest_first:
        if await scripts.repush_ticket(
            number_id, lead_id, lease["tk"], UNCLAIMED_REPUSH_MS, head
        ):
            repushed += 1
    return repushed


async def _reap(
    number_id: str,
    lead_id: str,
    lease: dict,
    state: Optional[LeadDispatchState],
    now_ms: int,
) -> bool:
    """Free a claimed or stuck lease's line by its lead's status (rule 14)."""
    status = state.status if state is not None else None
    if status == PROCESSING:
        # the call (or a held unknown dial) owns the line; only the lease goes. The
        # stuck-call check asks the provider whether it ended.
        return await scripts.clear_lease(number_id, lead_id, lease["tk"])
    # free the line; a lead still waiting goes back to its room
    requeue = lease["t"] if status == BACKLOG else ""
    due = max(now_ms, _ms(state.next_attempt_at if state else None))
    reply = await scripts.reap_lease(
        number_id,
        lead_id,
        lease["tk"],
        requeue,
        due,
        allow_dialling="dialling_ms" in lease,
        rank=scripts.rank_from_priority(state and state.priority),
    )
    if reply is None or reply == scripts.Reap.LEASE_CHANGED:
        return False
    if requeue and state is not None and state.is_locked:
        # Its coroutine died between the lead's lock and the dial: unlocked here, the
        # re-issued ticket can lock it, instead of re-queueing 30 s out until the 10-min
        # stale-lock clean (rule 25). Were a claimed one alive after all (180 s after its
        # claim, far past a slow pre-check and the greeting wait), its mark_dialling now
        # fails on the ticket id, so it never dials on this number. A lead can also hold
        # a ticket on another number (its template moved meanwhile); there, the lead's DB
        # lock taken before the dial, and mark_dialling's ticket check, let one of them dial.
        await release_lock_on_lead_by_id(lead_id)
    return True


async def reap_leases(
    max_age_ms: int = LEASE_MAX_AGE_MS, dial_stuck_ms: int = DIAL_STUCK_MS
) -> int:
    """Re-deliver unclaimed tickets and free lines held by dead ones (rule 14). Returns
    how many leases were re-delivered, reaped or cleared."""
    c = await _client()
    now_ms = _now_ms()
    old = await _old_leases(c, now_ms, max_age_ms, dial_stuck_ms)
    repushed = await _repush_unclaimed(c, old.unclaimed)
    for number_id, lead_id, lease in old.granted:
        # the grant worker lost it: sent again, or (BB_V2_GRANT_MAX_S) its line freed
        if await scripts.regrant(
            number_id, lead_id, lease["tk"], GRANT_RESEND_MS, GRANT_MAX_MS
        ):
            repushed += 1
    if old.granted:
        # every lease here waits for its lead row past BB_V2_GRANT_RESEND_S: the oldest
        # is what the alert reports (the bb:grants head is re-stamped by each re-send)
        number_id, _, lease = min(old.granted, key=lambda g: int(g[2]["issued_ms"]))
        age_s = (now_ms - int(lease["issued_ms"])) // 1000
        await raise_v2_grants_waiting(number_id, age_s, len(old.granted))
    dead = old.claimed + old.stuck
    if len(dead) > BREAKER_FLOOR:
        async with c.pipeline(transaction=False) as pipe:
            for number_id in await c.smembers(k.V2_ACTIVE_KEY):
                pipe.scard(k.busy_key(number_id))
            held = sum(await pipe.execute())
        if await _breaker_stops("lease_reaper", len(dead), held):
            dead = []
    reaped = 0
    if dead:
        states = await get_lead_dispatch_states([lead for _, lead, _ in dead])
        for number_id, lead_id, lease in dead:
            if await _reap(number_id, lead_id, lease, states.get(lead_id), now_ms):
                reaped += 1
    if reaped or repushed:
        logger.info(
            f"v2 lease reaper reaped {reaped} leases, re-delivered {repushed} tickets"
        )
    return reaped + repushed


# ---------------------------------------------------------------------------
# Seeding (used by switch.py)
# ---------------------------------------------------------------------------


async def locked_leads_by_number() -> Dict[str, Set[str]]:
    """Locked BACKLOG leads, by the number their template routes to now: today's workers
    past the redirect, maybe mid-dial. Only these can still become a legacy call once a
    number is ``v2_pending`` (an unlocked lead a worker picks is bounced to its room), so
    they alone are the switch-on signature and the seed's extra holders (Fable I4). Read
    once per switch step for every number (Fable I2). Each template is re-resolved first:
    a stale route would credit its leads to the wrong number. Raises if the DB can't be
    read."""
    number_of: Dict[str, Optional[str]] = {}
    by_number: Dict[str, Set[str]] = {}
    for lead_id, template_id, is_locked in await get_legacy_inflight_leads([]):
        if not is_locked or not template_id:
            continue
        if template_id not in number_of:
            # Strict: the reads behind the route answer None on a DB or Redis error too, so
            # an unresolved route fails the step and the next check retries. A lead dropped
            # here could be a legacy dial in flight that the seed would then miss.
            await invalidate_route(template_id, strict=True)
            route = await ensure_route(template_id)
            if route is None:
                raise RuntimeError(f"route of {template_id} unresolved; step retries")
            number_of[template_id] = route.number_id
        number_id = number_of[template_id]
        if number_id:
            by_number.setdefault(number_id, set()).add(lead_id)
    return by_number


def locked_signature(locked: Set[str]) -> str:
    """A digest of a number's locked leads, compared across switch checks."""
    return hashlib.sha1("\n".join(sorted(locked)).encode()).hexdigest()


async def seed_holders(number_id: str, locked: Optional[Set[str]] = None) -> Set[str]:
    """Who holds the number's lines right now: live outbound calls (``lead:``), live
    inbound calls (``call:``) and the number's locked BACKLOG leads (``lead:``; read here
    unless the caller read them earlier in the same step). Raises if the DB can't be read.

    The locked set is read FIRST (like rule 11): a legacy dial goes locked BACKLOG ->
    PROCESSING, so whichever read it falls between, one of them sees it. Live calls first
    could miss a dial turning PROCESSING between the two reads, and over-dial."""
    if locked is None:
        locked = (await locked_leads_by_number()).get(number_id, set())
    holders = {f"lead:{lead_id}" for lead_id in locked}
    for lead_id, direction, call_id in await get_live_calls_on_number(number_id):
        if direction == "INBOUND":
            if call_id:
                holders.add(f"call:{call_id}")
        else:
            holders.add(f"lead:{lead_id}")
    return holders


# ---------------------------------------------------------------------------
# Rank backfill
# ---------------------------------------------------------------------------


async def _rank_chunk(template_id: str, chunk: List[Tuple[str, int]]) -> int:
    if not chunk:
        return 0
    states = await get_lead_dispatch_states([lead_id for lead_id, _ in chunk])
    priority = {lead_id: state.priority for lead_id, state in states.items()}
    # no rank on its row: the rank its run gives it now, one ask for the chunk
    runs = [
        (lead_id, state.enrollment_id)
        for lead_id, state in states.items()
        if not state.priority and state.enrollment_id
    ]
    if runs:
        try:
            priority.update(await ranks_for_leads(runs))
        except Exception as e:  # noqa: BLE001 — they are left for the default rank
            # raising here would keep the number's backfill mark, and match promotes
            # nobody on it while the mark is set
            logger.error(f"v2 rank backfill: runs' ranks unread for {template_id}: {e}")
    rows = [
        (template_id, lead_id, due_ms, rank)
        for lead_id, due_ms in chunk
        for rank in [scripts.rank_from_priority(priority.get(lead_id))]
        if rank.rank
    ]
    await scripts.enqueue_many(rows, only_if_present=True)
    return len(rows)


async def backfill_ranks() -> int:
    """Leads queued before their number became ranked get the rank on their rows. The
    number-facts job marks such a number (bb:num:{N}.backfill) and match promotes nobody
    on it meanwhile, so none of them takes the default rank first; a lead with no rank on
    its row is left for promote. Rooms are read in ZSCAN chunks, like the prune; a run
    that fails leaves the mark and the next one goes on. Returns the leads ranked."""
    c = await _client()
    ranked = 0
    for number_id in sorted(await c.smembers(k.V2_ACTIVE_KEY)):
        if await c.hget(k.num_key(number_id), "backfill") != "1":
            continue
        for template_id in sorted(await c.smembers(k.numtpl_key(number_id))):
            chunk: List[Tuple[str, int]] = []
            async for lead_id, score in c.zscan_iter(
                k.room_key(template_id), count=BB_V2_PRUNE_CHUNK
            ):
                if score >= 0:  # not ranked yet: its score is its due time
                    chunk.append((lead_id, int(score)))
                if len(chunk) >= BB_V2_PRUNE_CHUNK:
                    ranked += await _rank_chunk(template_id, chunk)
                    chunk = []
            ranked += await _rank_chunk(template_id, chunk)
        await c.hdel(k.num_key(number_id), "backfill")
        # match issued nothing while the mark was set: the number is due at once
        await c.zadd(k.DUE_KEY, {number_id: int(time.time() * 1000)}, lt=True)
    if ranked:
        logger.info(f"v2 rank backfill ranked {ranked} queued leads")
    return ranked


# ---------------------------------------------------------------------------
# Orphan prune
# ---------------------------------------------------------------------------


async def _drop_finished(c: Any, room: str) -> int:
    """ZREM members whose lead is no longer BACKLOG (or no longer exists). The room is
    read in ZSCAN chunks of BB_V2_PRUNE_CHUNK, each checked against the DB and pruned
    before the next: a 100k-lead room is never one multi-MB reply (spec 2026-10-05 §4.10).
    A member ZSCAN returns twice is removed once (ZREM counts it once)."""
    dropped = 0
    chunk: List[str] = []
    async for member, _score in c.zscan_iter(room, count=BB_V2_PRUNE_CHUNK):
        chunk.append(member)
        if len(chunk) >= BB_V2_PRUNE_CHUNK:
            dropped += await _drop_chunk(c, room, chunk)
            chunk = []
    if chunk:
        dropped += await _drop_chunk(c, room, chunk)
    return dropped


async def _drop_chunk(c: Any, room: str, members: List[str]) -> int:
    states = await get_lead_dispatch_states(members)
    gone = [m for m in members if m not in states or states[m].status != BACKLOG]
    template_id = room[len(k.room_key("")) :]
    # its lead row exists now (a woken run made it): it is no longer a call with no row
    rowed = [m for m in members if m not in gone]
    if rowed:
        await c.hdel(k.qi_key(template_id), *rowed)
    # a workflow call with no lead row yet (bb:qi) stays: the CRM says when it left
    runs = await c.hmget(k.qi_key(template_id), gone) if gone else []
    gone = [m for m, run in zip(gone, runs) if not run]
    if not gone:
        return 0
    # with the ready score a ranked number remembered for it, and its run
    await c.hdel(k.qp_key(template_id), *gone)
    await c.hdel(k.qi_key(template_id), *gone)
    await c.hdel(k.qa_key(template_id), *gone)
    return await c.zrem(room, *gone)


async def set_aside_parked(c: Any, template_id: str) -> int:
    """Calls with no lead row leave the room (today's dialler cannot dial them); their
    bb:qi record stays for a manual recovery. Returns how many were set aside."""
    parked = await c.hkeys(k.qi_key(template_id))
    if not parked:
        return 0
    await c.zrem(k.room_key(template_id), *parked)
    logger.warning(
        f"v2: {len(parked)} calls with no lead row set aside from {template_id} "
        "(kept in bb:qi for a manual recovery)"
    )
    return len(parked)


async def prune_orphans() -> int:
    """Rooms whose template has no route, or whose number v2 doesn't account for, go back
    to today's schedule with their due times (Fable M4: today's "dial without template"
    behaviour stays). Other rooms lose leads that are no longer BACKLOG. Returns the number
    of leads moved or dropped."""
    c = await _client()
    changed = 0
    async for room in c.scan_iter(match=k.room_key("*"), count=500):
        template_id = room[len(k.room_key("")) :]
        try:
            is_v2 = await _is_v2_template(template_id)
            if is_v2 is None:
                continue  # can't tell this run
            if is_v2:
                changed += await _drop_finished(c, room)
            else:
                await set_aside_parked(c, template_id)  # recovered by hand, not here
                changed += await scripts.move_room_to_schedule(template_id) or 0
        except Exception as e:  # noqa: BLE001 — one room must not stop the rest
            logger.error(f"v2 prune failed for {room}: {e}")
    if changed:
        logger.info(f"v2 orphan prune moved or dropped {changed} leads")
    return changed

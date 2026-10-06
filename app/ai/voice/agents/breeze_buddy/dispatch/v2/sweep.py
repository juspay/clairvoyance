"""The 1 s sweep: the v2 dialler's clock (design card §5; Fable I3).

Runs on every dispatcher pod; only the leader (``bb:v2:sweep:leader``) ticks. A tick is
cheap: no SCAN, no sleeping, no DB. It matches every number with waiting leads in one
round trip (a due time arriving is the one event no code reacts to). Everything slower
is a job, spawned on a tick counter with its own timeout and error handling, and never
two copies of the same job at once.

Until v2 is first used (``v2_seen``) the sweeper does nothing but that latch check: no
leader key, no reads.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, NamedTuple, Optional, Set

from app.ai.voice.agents.breeze_buddy.dispatch.alerts import raise_v2_due_write_missed
from app.ai.voice.agents.breeze_buddy.dispatch.leader import LeaderElection
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import keys as k, scripts
from app.ai.voice.agents.breeze_buddy.dispatch.v2.latch import (
    epoch_lost,
    epoch_seen,
    v2_seen,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2.monitor import (
    check_sweep_leader,
    run_monitors,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2.reconcile import (
    backfill_ranks,
    ledger_check,
    prune_orphans,
    reap_leases,
    reconcile_backlog_v2,
    requeue_waiting_calls,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import (
    _client,
    invalidate_route,
    refresh_number,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2.switch import (
    recover_after_flush,
    run_switch_step,
)
from app.core.concurrency import spawn_background_task
from app.core.config import dynamic as dyn_cfg
from app.core.config.static import (
    BB_V2_DUE_BATCH,
    BB_V2_DUE_FULL_PASS_TICKS,
    BB_V2_MATCH_CAP,
    BB_V2_RECONNECT_GRACE_S,
    BB_V2_ROUTES_REFRESH_S,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.dispatch import (
    count_processing_by_telephony_number,
    get_telephony_numbers_by_ids,
    set_telephony_number_channels,
)

SWEEP_INTERVAL_S = 1.0
LEADER_CHECK_EVERY = 5  # loops; every pod, since a missing leader can't report itself


# ---------------------------------------------------------------------------
# Jobs (each runs alone, spawned by the leader's tick)
# ---------------------------------------------------------------------------


async def refresh_enabled_mirror() -> None:
    """Mirror the kill switch (dynamic config) into Redis for ``match`` (rule 6). Today's
    reseller pause keys need no mirror: ``match`` reads them itself (Fable M2). Switched
    back on, every v2-accounted number is due now: match did nothing while it was off,
    not even after a call freed a line."""
    c = await _client()
    enabled = await dyn_cfg.BB_DISPATCH_ENABLED()
    was = await c.set(k.ENABLED_MIRROR_KEY, "1" if enabled else "0", get=True)
    if enabled and was == "0":
        active = await c.smembers(k.V2_ACTIVE_KEY)
        if active:
            now_ms = int(time.time() * 1000)
            await c.zadd(k.DUE_KEY, {n: now_ms for n in active}, lt=True)


_warned_shared: Set[str] = set()  # numbers of no merchant listed as ranked: said once


async def refresh_number_facts() -> None:
    """``max`` / status / provider of every v2-accounted number, from the DB (rule 13),
    and its ``ranked`` and ``intents`` flags from BB_V2_RANKED_NUMBERS and
    BB_V2_INTENT_NUMBERS: a list that can't be read changes no flag. An intents number
    is always ranked too."""
    c = await _client()
    active = sorted(await c.smembers(k.V2_ACTIVE_KEY))
    try:
        listed: Optional[List[str]] = await dyn_cfg.BB_V2_RANKED_NUMBERS(strict=True)
    except Exception as e:  # noqa: BLE001 — a blip must not unrank every number
        listed = None
        logger.warning(f"v2 ranked numbers unread, flags unchanged: {e}")
    try:
        intents: Optional[List[str]] = await dyn_cfg.BB_V2_INTENT_NUMBERS(strict=True)
    except Exception as e:  # noqa: BLE001 — as above
        intents = None
        logger.warning(f"v2 intent numbers unread, flags unchanged: {e}")
    for number_id, number in (await get_telephony_numbers_by_ids(active)).items():
        await refresh_number(number)
        key = k.num_key(number_id)
        own = bool(getattr(number, "merchant_id", None))
        was_intents, was_ranked = await c.hmget(key, "intents", "ranked")
        # new calls with no lead row: never on a number of no merchant (shared)
        on = was_intents == "1" if intents is None else number_id in intents and own
        ranked = was_ranked == "1" if listed is None else number_id in listed
        if ranked and not own:
            # ranks are one merchant's: on a shared number they would starve the others
            ranked = False
            if number_id not in _warned_shared:
                _warned_shared.add(number_id)
                logger.warning(
                    f"v2: {number_id} is listed in BB_V2_RANKED_NUMBERS but belongs "
                    "to no merchant; it stays unranked"
                )
        if on and not ranked:
            # a call with no lead row brings its rank, and only a ranked number keeps it
            ranked = True
            if was_ranked != "1":
                logger.warning(
                    f"v2: {number_id} is in BB_V2_INTENT_NUMBERS but not in "
                    "BB_V2_RANKED_NUMBERS; it is ranked all the same"
                )
        flags = {"intents": "1" if on else "0", "ranked": "1" if ranked else "0"}
        if ranked and was_ranked != "1":
            # leads already queued get their rows' ranks first (backfill_ranks)
            flags["backfill"] = "1"
        # one write: a number never takes calls with no lead row while unranked
        await c.hset(key, mapping=flags)


async def refresh_routes() -> None:
    """Re-resolve the routes of templates with waiting leads on v2-accounted numbers,
    every BB_V2_ROUTES_REFRESH_S: catches template, config and number edits made without
    a save hook (design card §5). An empty room needs none: its next lead is routed by
    the route as it is then, and this pass sees it once leads wait. Two round trips, then
    one re-resolve per template with leads."""
    c = await _client()
    numbers = sorted(await c.smembers(k.V2_ACTIVE_KEY))
    if not numbers:
        return
    async with c.pipeline(transaction=False) as pipe:
        for number_id in numbers:
            pipe.smembers(k.numtpl_key(number_id))
        templates = sorted({t for listed in await pipe.execute() for t in listed})
    async with c.pipeline(transaction=False) as pipe:
        for template_id in templates:
            pipe.zcard(k.room_key(template_id))
        sizes = await pipe.execute()
    for template_id, size in zip(templates, sizes):
        if size:
            await invalidate_route(template_id)


async def write_channels_mirror() -> None:
    """DB ``telephony_numbers.channels`` of numbers in mode ``v2`` = the DB's own count of
    calls holding a line there: the query today's token reconciler and the hand-back use,
    never the busy list's size (which also counts tickets). Today's code only moves the
    column by +-1, so this keeps today's gate right even when v2 lets go of a number with
    no hand-back (a Redis flush, a code rollback): at most one run stale, like today's
    own drift (Fable I3). The hand-back writes the same count itself."""
    c = await _client()
    active = sorted(await c.smembers(k.V2_ACTIVE_KEY))
    if not active:
        return
    in_flight = await count_processing_by_telephony_number()
    for number_id in active:
        # Read right before this number's write, never once up front: at the DB's busiest
        # the loop can outlast a global off's drain + hand-back, and once a hand-back has
        # begun (mode no longer v2) it owns the column. An older count written over its
        # own would leave today's gate wrong for good.
        if await c.hget(k.num_key(number_id), "mode") == "v2":
            await set_telephony_number_channels(number_id, in_flight.get(number_id, 0))


async def backlog_job() -> None:
    """The backlog reconciler, only while some number is v2-accounted (it pages the DB)."""
    c = await _client()
    if await c.scard(k.V2_ACTIVE_KEY):
        await reconcile_backlog_v2()


async def waiting_calls_job() -> None:
    """Waiting calls with no lead row go back in their rooms, only while some number
    takes such calls (it pages the CRM's parked runs)."""
    c = await _client()
    for number_id in await c.smembers(k.V2_ACTIVE_KEY):
        if await c.hget(k.num_key(number_id), "intents") == "1":
            return await requeue_waiting_calls()


class Job(NamedTuple):
    name: str
    every: int  # ticks (seconds)
    fn: Callable[[], Awaitable[Any]]
    timeout_s: float


JOBS = (
    Job("switch", 5, run_switch_step, 30),
    Job("enabled_mirror", 5, refresh_enabled_mirror, 5),
    Job("number_facts", 5, refresh_number_facts, 5),
    Job("rank_backfill", 5, backfill_ranks, 120),
    Job("ledger", 30, ledger_check, 25),
    # every 5 s: a line waiting for its lead row is sent again after BB_V2_GRANT_RESEND_S
    Job("lease_reaper", 5, reap_leases, 25),
    Job("channels_mirror", 30, write_channels_mirror, 25),
    Job("backlog", 60, backlog_job, 55),
    Job("waiting_calls", 60, waiting_calls_job, 55),
    # a run may take up to half its interval (thousands of templates, a DB read each)
    Job("routes", BB_V2_ROUTES_REFRESH_S, refresh_routes, BB_V2_ROUTES_REFRESH_S / 2),
    Job("orphan_prune", 300, prune_orphans, 120),
    Job("monitor", 15, run_monitors, 10),
)


# ---------------------------------------------------------------------------
# The sweeper
# ---------------------------------------------------------------------------


class Sweeper:
    def __init__(self, redis_client: Any = None):
        self._redis = redis_client  # tests pass a client; production uses the service
        self._leader = LeaderElection(
            key=k.SWEEP_LEADER_KEY, task_name="bb-v2-sweep-leader"
        )
        self._stopping = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._ticks = 0
        self._loops = 0
        self._running: Dict[str, asyncio.Task] = {}
        # monotonic time this leader first saw bb:epoch missing; None while it is set
        self._lost_since: Optional[float] = None
        # monotonic time until which the jobs that free lines are not started
        # (BB_V2_RECONNECT_GRACE_S after a failed tick or a Redis-loss recovery)
        self._grace_until = 0.0

    async def _client(self) -> Any:
        return self._redis if self._redis is not None else await _client()

    async def tick(self) -> None:
        if not await v2_seen():
            return
        c = await self._client()
        present = bool(await c.exists(k.EPOCH_KEY))
        lost = epoch_lost(present)  # also notes that this process has seen it set
        if not present:
            await self._recover(first_use=not lost)
            return
        self._lost_since = None
        self._ticks += 1
        await self._match_due(c)
        waiting = time.monotonic() < self._grace_until
        for job in JOBS:
            if self._ticks % job.every == 0 and not (
                waiting and job.name in ("ledger", "lease_reaper")
            ):
                self._spawn(job)

    async def _match_due(self, c: Any) -> None:
        """Match the numbers whose ``bb:due`` time has come (design card rule 55), and on
        every BB_V2_DUE_FULL_PASS_TICKS-th tick every v2-accounted number too: the safety
        net, which logs any number it found dialable that ``bb:due`` did not list."""
        now_ms = int(time.time() * 1000)
        due: Set[str] = set(
            await c.zrangebyscore(
                k.DUE_KEY, "-inf", now_ms, start=0, num=BB_V2_DUE_BATCH
            )
        )
        unlisted: Dict[str, Optional[float]] = {}
        if self._ticks % BB_V2_DUE_FULL_PASS_TICKS == 0:
            # their bb:due scores now, before match rewrites them: one that is due but
            # was past BB_V2_DUE_BATCH is skipped by the cap, not missed
            extra = sorted(set(await c.smembers(k.V2_ACTIVE_KEY)) - due)
            if extra:
                unlisted = dict(zip(extra, await c.zmscore(k.DUE_KEY, extra)))
        # one round trip for every number (spec 2026-10-05 §4.10); one whose run hit the
        # cap (a window opening on a big pile) is filled now, not 100 lines a second
        issued_by = await scripts.match_many(sorted(due | set(unlisted)))
        for number_id, issued in issued_by.items():
            if issued == BB_V2_MATCH_CAP:
                await scripts.match_all(number_id)
        missed = [
            n
            for n, score in unlisted.items()
            if issued_by.get(n) and (score is None or score > now_ms)
        ]
        if missed:
            logger.warning(
                f"v2 sweep: the full pass issued tickets on {missed}, which bb:due "
                "did not list as due (a missed bb:due write, or one in flight)"
            )
            await raise_v2_due_write_missed(missed)

    async def _recover(self, first_use: bool) -> None:
        """``bb:epoch`` is missing. ``first_use``: this process never saw it set, so v2
        never ran and the epoch is set in this same tick. Otherwise Redis lost v2's keys
        and today's workers hold meanwhile: desired numbers restart at v2_pending in this
        same tick; with none (the flags went too), today's counters are rebuilt from the
        DB once the loss is ``switch.LEGACY_RECOVERY_WAIT_MS`` old. Then the rooms are
        refilled from the DB at once."""
        now = time.monotonic()
        if self._lost_since is None:
            self._lost_since = now
        lost_for_ms = int((now - self._lost_since) * 1000)
        if not await recover_after_flush(lost_for_ms=lost_for_ms, first_use=first_use):
            return
        self._lost_since = None
        self._grace_until = time.monotonic() + BB_V2_RECONNECT_GRACE_S
        for job in JOBS:
            if job.name == "backlog":
                self._spawn(job)

    async def _learn_epoch(self) -> None:
        """Every pod, not only the leader, must have seen ``bb:epoch`` set to tell a later
        loss from first use: its workers may only meet v2 numbers, whose dial path never
        reads it. One EXISTS a loop until then, none after. Best effort: a failed read
        must not stop this pod's leader check; the next loop reads again."""
        if epoch_seen():
            return
        try:
            c = await self._client()
            epoch_lost(bool(await c.exists(k.EPOCH_KEY)))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"v2 epoch read failed: {e}")

    def _spawn(self, job: Job) -> None:
        running = self._running.get(job.name)
        if running is not None and not running.done():
            return  # never two copies of the same job
        self._running[job.name] = spawn_background_task(
            self._run_job(job), name=f"bb-v2-{job.name}"
        )

    def _cancel_jobs(self) -> None:
        """Not (or no longer) the leader: stop this pod's jobs. A deposed leader must not
        finish a switch step or a counter rewrite its successor runs too; every job is
        idempotent and the new leader re-runs it (review #1287 finding 3)."""
        for name, task in list(self._running.items()):
            if not task.done():
                task.cancel()
                logger.warning(f"v2 sweep: {name} job cancelled, no longer the leader")

    @staticmethod
    async def _run_job(job: Job) -> None:
        try:
            await asyncio.wait_for(job.fn(), timeout=job.timeout_s)
        except Exception as e:  # noqa: BLE001 — incl. timeout; the next run retries
            logger.error(f"v2 {job.name} job failed: {type(e).__name__}: {e}")

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                if await v2_seen():
                    await self._leader.start()  # no-op once started
                    if self._leader.is_leader:
                        await self.tick()
                    else:
                        self._lost_since = None  # a later lead must not reuse it
                        self._cancel_jobs()
                        await self._learn_epoch()
                    self._loops += 1
                    if self._loops % LEADER_CHECK_EVERY == 0:
                        await check_sweep_leader()
            except Exception as e:  # noqa: BLE001 — the clock must keep running
                logger.error(f"v2 sweep tick failed: {e}")
                self._grace_until = time.monotonic() + BB_V2_RECONNECT_GRACE_S
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=SWEEP_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="bb-v2-sweep")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await asyncio.wait({self._task}, timeout=SWEEP_INTERVAL_S + 2)
        await self._leader.stop()
        for task in self._running.values():
            task.cancel()  # every job is idempotent; the next leader re-runs it

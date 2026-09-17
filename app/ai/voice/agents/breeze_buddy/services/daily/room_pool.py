"""Pre-created Daily rooms, so a voice session never waits on the Daily API.

Creating a room and minting its two meeting tokens costs ~476ms even on a warm
connection (~1.4s on a cold one) — three round trips to api.daily.co that the
caller pays before the browser is told where to connect. None of that work
depends on the lead: a room is an anonymous container. So it is done in advance
and the request just takes one off a queue.

  create room + tokens, on demand   ~476 ms
  take a pre-created room             <1 ms

**Two pools, not one.** ``enable_recording`` is a per-call decision — telephony
and demo record, widget voice does not — and it changes both the room
(``enable_recording="cloud"``) and the bot token (``start_cloud_recording``).
Those could be collapsed into one pool by always permitting recording and
varying only the token, but the user token is minted with ``owner=True``, so a
widget participant could then start a recording on a call that is meant not to
be recorded. Two small pools keep every room byte-identical to what the
on-demand path builds today.

**Expiry.** A room that sat in the pool still has to survive the call that
takes it. Pooled rooms and tokens are created with
``BB_DAILY_ROOM_POOL_TTL_SECS`` of life, and a room is only handed out while it
has at least ``BB_DAILY_ROOM_MIN_REMAINING_SECS`` left — the same hour the
on-demand path gives. Anything staler is dropped and replaced, so dwell time in
the pool never eats into a call.

**Never worse than today.** An empty pool, a disabled pool, a refill that
cannot reach Daily — every one of them falls through to creating the room
inline, which is exactly the current behaviour. The pool is a fast path, never
a dependency.

Sizing
------
Target: at most 1 call in 100 waits on the Daily API.

Taking a room makes it unavailable until it is rebuilt, so the pool is an
Erlang-B loss system: N "servers", offered load ``a = lambda * T`` Erlangs,
and a miss is an arrival finding all N out for rebuild. Measured service time
is 486ms solo (p99 625ms), degrading to ~6.5 builds/sec under concurrency.
Peak arrival rate follows from Little's Law at the capacity ceiling,
``lambda = BB_MAX_CONCURRENT_DAILY_BOTS / call_length``:

===========  ===========  =======  ============
call length  peak rate    load a   N for <=1%
===========  ===========  =======  ============
30s          0.667/s      0.324    3
60s          0.333/s      0.162    3
120s         0.167/s      0.081    2
300s         0.067/s      0.032    2
===========  ===========  =======  ============

Closed-form Erlang-B and a discrete-event simulation of the measured
contention agree on every row. So for *independent* arrivals three rooms
would do.

The default is nevertheless BB_MAX_CONCURRENT_DAILY_BOTS, because that table
is only valid while arrivals are independent, and the same simulation shows
how fast that assumption fails: with requests landing in correlated groups of
2 it needs 4 rooms, groups of 5 need 8, groups of 10 need 14. A burst of k
simultaneous requests strands ``k - N`` of them outright regardless of rate.
Sizing to the ceiling makes the answer independent of the arrival model, and
it is free: rooms are built concurrently, so 20 fill in 1244ms against 1175ms
for 3, Daily bills participant-minutes rather than rooms, and past the ceiling
the capacity gate rejects the session anyway — so no burst this pod will ever
admit can exceed the pool.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from pipecat.transports.daily.utils import (
    DailyMeetingTokenParams,
    DailyMeetingTokenProperties,
    DailyRESTHelper,
    DailyRoomParams,
    DailyRoomProperties,
)

from app.ai.voice.agents.breeze_buddy.services.daily._pools import (
    call_with_retry,
    get_daily_rest_session,
)
from app.core.config.dynamic import BB_DAILY_ROOM_POOL
from app.core.config.static import (
    BB_DAILY_ROOM_MIN_REMAINING_SECS,
    BB_DAILY_ROOM_POOL_IDLE_SECS,
    BB_DAILY_ROOM_POOL_MAX,
    BB_DAILY_ROOM_POOL_MIN,
    BB_DAILY_ROOM_POOL_REFILL_INTERVAL_SECS,
    BB_DAILY_ROOM_POOL_TARGET_MISS,
    BB_DAILY_ROOM_POOL_TTL_SECS,
    BREEZE_BUDDY_DAILY_API_KEY,
    BREEZE_BUDDY_DAILY_API_URL,
)
from app.core.logger import logger

__all__ = [
    "PooledRoom",
    "Demand",
    "target_depth",
    "acquire_room",
    "pool_depth",
    "start_room_pool",
    "stop_room_pool",
]


@dataclass(frozen=True)
class PooledRoom:
    """One ready-to-use room: what start_daily_session would have built."""

    room_url: str
    room_name: str
    user_token: str
    bot_token: str
    expires_at: float

    def usable_for_a_call(self) -> bool:
        return self.expires_at - time.time() >= BB_DAILY_ROOM_MIN_REMAINING_SECS


# One queue per recording flavour. Keyed by enable_recording.
_pools: Dict[bool, "asyncio.Queue[PooledRoom]"] = {}
_refill_task: Optional[asyncio.Task] = None
# Rooms currently being built, so concurrent refills do not overshoot.
_inflight: Dict[bool, int] = {True: 0, False: 0}
# Strong refs to eager refills; asyncio keeps only weak ones.
_refills: set = set()


# ---------------------------------------------------------------------------
# sizing controller
# ---------------------------------------------------------------------------
#
# Each flavour sizes itself from its own traffic. Taking a room makes it
# unavailable until rebuilt, so depth is an Erlang-B loss system: offered load
# ``a = lambda * T_build`` Erlangs, and the target is the smallest depth whose
# blocking probability is under BB_DAILY_ROOM_POOL_TARGET_MISS.
#
# Erlang-B alone is not enough, because it assumes independent arrivals. A
# burst of k simultaneous requests strands k - depth of them at any rate, so
# the recent observed peak is carried as a floor.
#
# Growth and shrink are deliberately asymmetric. A room costs nothing to hold
# — Daily bills participant-minutes, not rooms — while running short costs a
# caller ~476ms. So the pool jumps straight to whatever demand asks for and
# gives depth back one room at a time, and only after the traffic that
# justified it has actually gone.


@dataclass
class Demand:
    """What one flavour's traffic looks like right now."""

    rate_ewma: float = 0.0  # takes per second
    takes_this_tick: int = 0
    recent_peak: int = 0  # most takes seen in a single tick lately
    peak_age_ticks: int = 0
    last_take: float = 0.0
    depth: int = 0  # what the controller currently asks for


_demand: Dict[bool, Demand] = {True: Demand(), False: Demand()}

# How fast the rate estimate follows traffic. 0.3 over a 10s tick reaches ~90%
# of a step change in about a minute — fast enough for a rush, slow enough not
# to chase a single arrival.
# Measured: one room plus its two tokens takes ~486ms on the warm
# connection (p99 625ms). This is the service time in the Erlang term.
_BUILD_SECS = 0.486
_RATE_ALPHA = 0.3
# A peak is honoured for this many ticks after it is seen, then decays. Long
# enough that a burst does not have to repeat to keep its headroom.
_PEAK_TTL_TICKS = 6


def _erlang_b(depth: int, load: float) -> float:
    """Blocking probability for ``depth`` servers at ``load`` Erlangs.

    Iterative form: the textbook factorial ratio overflows well before the
    depths we care about.
    """
    inv = 1.0
    for k in range(1, depth + 1):
        inv = 1.0 + inv * k / load
    return 1.0 / inv


def _erlang_depth(load: float, target_miss: float, ceiling: int) -> int:
    """Smallest depth whose blocking probability is at or under target."""
    if load <= 0:
        return 0
    for depth in range(1, ceiling + 1):
        if _erlang_b(depth, load) <= target_miss:
            return depth
    return ceiling


def target_depth(demand: Demand, *, now: float, build_secs: float) -> int:
    """How many rooms this flavour should hold.

    Pure so the policy can be tested without a pool, a clock or Daily.
    """
    if BB_DAILY_ROOM_POOL_MAX <= 0:
        return 0
    # Gone quiet: hold nothing. The next caller pays one inline build.
    if demand.last_take and now - demand.last_take > BB_DAILY_ROOM_POOL_IDLE_SECS:
        return 0

    # What independent arrivals at the current rate need...
    by_rate = _erlang_depth(
        demand.rate_ewma * build_secs,
        BB_DAILY_ROOM_POOL_TARGET_MISS,
        BB_DAILY_ROOM_POOL_MAX,
    )
    # ...and what the worst clump actually asked for, which Erlang-B cannot see.
    by_burst = demand.recent_peak if demand.peak_age_ticks < _PEAK_TTL_TICKS else 0
    return min(max(BB_DAILY_ROOM_POOL_MIN, by_rate, by_burst), BB_DAILY_ROOM_POOL_MAX)


def _observe_take(enable_recording: bool) -> None:
    demand = _demand[enable_recording]
    demand.takes_this_tick += 1
    demand.last_take = time.monotonic()


def _advance_controller(enable_recording: bool, *, build_secs: float) -> int:
    """Fold one tick of observations into the target. Returns the new target."""
    demand = _demand[enable_recording]
    tick = max(BB_DAILY_ROOM_POOL_REFILL_INTERVAL_SECS, 0.001)

    demand.rate_ewma = (1 - _RATE_ALPHA) * demand.rate_ewma + _RATE_ALPHA * (
        demand.takes_this_tick / tick
    )
    if demand.takes_this_tick >= demand.recent_peak:
        demand.recent_peak = demand.takes_this_tick
        demand.peak_age_ticks = 0
    else:
        demand.peak_age_ticks += 1
        if demand.peak_age_ticks >= _PEAK_TTL_TICKS:
            demand.recent_peak = demand.takes_this_tick
            demand.peak_age_ticks = 0
    demand.takes_this_tick = 0

    wanted = target_depth(demand, now=time.monotonic(), build_secs=build_secs)
    if wanted > demand.depth:
        demand.depth = wanted  # grow at once: a short pool costs a caller
    elif wanted < demand.depth:
        demand.depth -= 1  # give depth back slowly
    return demand.depth


def _queue(enable_recording: bool) -> "asyncio.Queue[PooledRoom]":
    queue = _pools.get(enable_recording)
    if queue is None:
        queue = asyncio.Queue(maxsize=max(1, BB_DAILY_ROOM_POOL_MAX))
        _pools[enable_recording] = queue
    return queue


def pool_depth(enable_recording: bool) -> int:
    """Rooms ready right now for this flavour. For logging and tests."""
    queue = _pools.get(enable_recording)
    return 0 if queue is None else queue.qsize()


async def create_room_now(enable_recording: bool, *, ttl_secs: float) -> PooledRoom:
    """Build one room and its two tokens. The on-demand path uses this too.

    Identical to what start_daily_session built inline before this module
    existed, so a pooled room and an on-demand room are indistinguishable.
    """
    daily_rest = DailyRESTHelper(
        daily_api_key=BREEZE_BUDDY_DAILY_API_KEY,
        daily_api_url=BREEZE_BUDDY_DAILY_API_URL,
        aiohttp_session=get_daily_rest_session(),
    )

    expires_at = time.time() + ttl_secs
    room_properties_kwargs: Dict[str, Any] = {
        "exp": expires_at,
        "eject_at_room_exp": True,
    }
    if enable_recording:
        room_properties_kwargs["enable_recording"] = "cloud"
    room_params = DailyRoomParams(
        properties=DailyRoomProperties(**room_properties_kwargs)
    )
    room = await call_with_retry(
        lambda: daily_rest.create_room(room_params), what="create_room"
    )

    bot_params = (
        DailyMeetingTokenParams(
            properties=DailyMeetingTokenProperties(start_cloud_recording=True)
        )
        if enable_recording
        else None
    )
    # Both tokens at once: they depend on room.url and not on each other.
    user_token, bot_token = await asyncio.gather(
        call_with_retry(
            lambda: daily_rest.get_token(room.url, expiry_time=ttl_secs),
            what="get_token(user)",
        ),
        call_with_retry(
            lambda: daily_rest.get_token(
                room.url, expiry_time=ttl_secs, params=bot_params
            ),
            what="get_token(bot)",
        ),
        return_exceptions=True,
    )
    if isinstance(user_token, BaseException):
        raise user_token
    if isinstance(bot_token, BaseException):
        raise bot_token

    return PooledRoom(
        room_url=room.url,
        room_name=room.name,
        user_token=user_token,
        bot_token=bot_token,
        expires_at=expires_at,
    )


async def acquire_room(enable_recording: bool) -> PooledRoom:
    """Hand out a ready room, or build one inline if none is ready.

    Stale rooms are discarded rather than returned: a room with less life left
    than a call needs would expire mid-conversation and eject everyone.
    """
    _observe_take(enable_recording)
    queue = _pools.get(enable_recording)
    while queue is not None and not queue.empty():
        room = queue.get_nowait()
        if room.usable_for_a_call():
            logger.debug(
                f"Daily room pool hit (recording={enable_recording}, "
                f"{queue.qsize()} left)"
            )
            _refill_soon(enable_recording)
            return room
        logger.info(f"Daily room pool: dropping stale room {room.room_name}")
        await _delete_room(room.room_name)

    logger.info(
        f"Daily room pool empty (recording={enable_recording}); creating inline"
    )
    _refill_soon(enable_recording)
    return await create_room_now(
        enable_recording, ttl_secs=BB_DAILY_ROOM_MIN_REMAINING_SECS
    )


async def _delete_room(room_name: str) -> None:
    """Best effort: an undeleted room only lingers until its own expiry."""
    session = get_daily_rest_session()
    url = f"{BREEZE_BUDDY_DAILY_API_URL.rstrip('/')}/rooms/{room_name}"
    try:
        async with session.delete(
            url, headers={"Authorization": f"Bearer {BREEZE_BUDDY_DAILY_API_KEY}"}
        ) as resp:
            await resp.read()
    except Exception as exc:  # noqa: BLE001 - cleanup must never raise
        logger.warning(f"Daily room pool: could not delete {room_name}: {exc!r}")


async def _top_up(enable_recording: bool) -> None:
    """Refill to size, building the missing rooms concurrently.

    Concurrently because serially is the difference between a pool of 20
    taking ~9s to fill and ~1s: each room is three round trips that block on
    the network, not the CPU, and they ride the shared connection pool.
    """
    queue = _queue(enable_recording)
    depth = _demand[enable_recording].depth
    missing = depth - queue.qsize() - _inflight[enable_recording]
    if missing <= 0:
        return

    _inflight[enable_recording] += missing
    try:
        results = await asyncio.gather(
            *(
                create_room_now(enable_recording, ttl_secs=BB_DAILY_ROOM_POOL_TTL_SECS)
                for _ in range(missing)
            ),
            return_exceptions=True,
        )
    finally:
        _inflight[enable_recording] -= missing

    failures = []
    for result in results:
        if isinstance(result, BaseException):
            failures.append(result)
            continue
        try:
            queue.put_nowait(result)
        except asyncio.QueueFull:
            # Raced with another refill; surplus, not stale.
            await _delete_room(result.room_name)
    if failures:
        logger.warning(
            f"Daily room pool: {len(failures)}/{len(results)} rooms failed to "
            f"build (recording={enable_recording}): {failures[0]!r}"
        )


def _refill_soon(enable_recording: bool) -> None:
    """Start replacing a room the instant it is taken.

    Without this the pool only recovers on the next tick of the refill loop,
    so a burst that drains it leaves every following caller creating rooms
    inline for up to BB_DAILY_ROOM_POOL_REFILL_INTERVAL_SECS.
    """

    async def _run() -> None:
        try:
            await _top_up(enable_recording)
        except Exception as exc:  # noqa: BLE001 - a refill is best effort
            logger.warning(f"Daily room pool: eager refill failed: {exc!r}")

    # Strong reference: asyncio only holds weak ones (Ruff RUF006).
    task = asyncio.create_task(_run())
    _refills.add(task)
    task.add_done_callback(_refills.discard)


async def _shed_surplus(enable_recording: bool) -> None:
    """Delete rooms the controller no longer wants.

    Held rooms cost nothing to Daily, but an unbounded drift of them clutters
    the account and they would each be replaced on expiry forever.
    """
    queue = _queue(enable_recording)
    depth = _demand[enable_recording].depth
    while queue.qsize() > depth:
        await _delete_room(queue.get_nowait().room_name)


async def _refill_loop() -> None:
    while True:
        # Read the flag every tick rather than at startup, so ops can turn the
        # pool on when traffic justifies it — and off again — without a deploy.
        try:
            enabled = await BB_DAILY_ROOM_POOL()
        except Exception as exc:  # noqa: BLE001 - config trouble must not
            logger.warning(f"Daily room pool: flag read failed ({exc!r}); staying off")
            enabled = False

        for enable_recording in (True, False):
            try:
                if not enabled:
                    # Drain and hold nothing. acquire_room then finds an empty
                    # queue and builds inline, which is the pre-pool path.
                    if _demand[enable_recording].depth or pool_depth(enable_recording):
                        logger.info(
                            f"Daily room pool disabled by config; draining "
                            f"(recording={enable_recording})"
                        )
                    _demand[enable_recording].depth = 0
                    await _shed_surplus(enable_recording)
                    continue
                before = _demand[enable_recording].depth
                after = _advance_controller(enable_recording, build_secs=_BUILD_SECS)
                if after != before:
                    logger.info(
                        f"Daily room pool depth {before} -> {after} "
                        f"(recording={enable_recording}, "
                        f"rate={_demand[enable_recording].rate_ewma:.4f}/s, "
                        f"peak={_demand[enable_recording].recent_peak})"
                    )
                await _shed_surplus(enable_recording)
                await _top_up(enable_recording)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop outlives failures
                logger.warning(
                    f"Daily room pool refill failed "
                    f"(recording={enable_recording}): {exc!r}"
                )
        await asyncio.sleep(BB_DAILY_ROOM_POOL_REFILL_INTERVAL_SECS)


def start_room_pool() -> None:
    """Begin filling the pool. Wired into FastAPI lifespan startup.

    Per-pod on purpose, like the connection warmer: rooms are handed to callers
    this pod serves, and the BackgroundTaskScheduler would run this on exactly
    one pod in the fleet.
    """
    global _refill_task

    if BB_DAILY_ROOM_POOL_MAX <= 0:
        logger.info("Daily room pool disabled (max=0); rooms created on demand")
        return
    for flavour in (True, False):
        _demand[flavour] = Demand(depth=BB_DAILY_ROOM_POOL_MIN)
    if _refill_task is not None and not _refill_task.done():
        return
    _refill_task = asyncio.create_task(_refill_loop())
    logger.info(
        f"Daily room pool starting (depth {BB_DAILY_ROOM_POOL_MIN}..."
        f"{BB_DAILY_ROOM_POOL_MAX} per flavour, sized from demand, "
        f"ttl={BB_DAILY_ROOM_POOL_TTL_SECS}s)"
    )


async def stop_room_pool() -> None:
    """Stop refilling and delete what was never used.

    Unused rooms would otherwise sit on the account until their own expiry;
    a pod restarting on every deploy would leave a trail of them.
    """
    global _refill_task

    task, _refill_task = _refill_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    for queue in _pools.values():
        while not queue.empty():
            await _delete_room(queue.get_nowait().room_name)
    _pools.clear()
    logger.info("Daily room pool stopped; unused rooms released")

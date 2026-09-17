"""Pre-created Daily rooms.

Creating a room and its two tokens costs ~476ms of round trips that do not
depend on the lead, so the pool does that work before the request arrives.
What these tests pin is the part that can go wrong quietly: a room must never
be handed out with less life left than the call needs, the two recording
flavours must never be mixed, and every failure path must fall through to
creating the room inline rather than failing the session.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.ai.voice.agents.breeze_buddy.services.daily import room_pool as rp


def _room(name: str = "r1", *, expires_in: float = 7200.0) -> rp.PooledRoom:
    return rp.PooledRoom(
        room_url=f"https://example.daily.co/{name}",
        room_name=name,
        user_token="user-tok",
        bot_token="bot-tok",
        expires_at=time.time() + expires_in,
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    rp._pools.clear()
    rp._refill_task = None
    for flavour in (True, False):
        rp._demand[flavour] = rp.Demand(depth=rp.BB_DAILY_ROOM_POOL_MIN)
    deleted: list = []

    async def fake_delete(name: str) -> None:
        deleted.append(name)

    monkeypatch.setattr(rp, "_delete_room", fake_delete)
    yield deleted
    rp._pools.clear()


# ---------------------------------------------------------------------------
# handing rooms out
# ---------------------------------------------------------------------------


async def test_a_ready_room_is_handed_over_without_touching_daily(monkeypatch):
    """The whole point: no API call on the request path."""

    async def boom(*a, **k):
        raise AssertionError("created a room despite one being ready")

    monkeypatch.setattr(rp, "create_room_now", boom)
    rp._queue(True).put_nowait(_room("ready"))

    got = await rp.acquire_room(True)
    assert got.room_name == "ready"
    assert rp.pool_depth(True) == 0


async def test_an_empty_pool_creates_the_room_inline(monkeypatch):
    """Never worse than the pre-pool behaviour."""
    calls = []

    async def fake_create(enable_recording, *, ttl_secs):
        calls.append((enable_recording, ttl_secs))
        return _room("inline")

    monkeypatch.setattr(rp, "create_room_now", fake_create)
    got = await rp.acquire_room(True)
    assert got.room_name == "inline"
    assert calls == [(True, rp.BB_DAILY_ROOM_MIN_REMAINING_SECS)]


async def test_the_two_recording_flavours_never_mix(monkeypatch):
    """A recorded call must not get a room that forbids recording, and a widget
    call must not get one whose bot token starts a recording."""
    created = []

    async def fake_create(enable_recording, *, ttl_secs):
        created.append(enable_recording)
        return _room("fresh")

    monkeypatch.setattr(rp, "create_room_now", fake_create)
    rp._queue(False).put_nowait(_room("no-recording"))

    # Asking for a recorded room must not drain the non-recording queue.
    await rp.acquire_room(True)
    assert created == [True]
    assert rp.pool_depth(False) == 1


# ---------------------------------------------------------------------------
# expiry — the failure that would only show up mid-call
# ---------------------------------------------------------------------------


async def test_a_room_too_close_to_expiry_is_never_handed_out(monkeypatch, _clean):
    """It would expire mid-conversation and eject everyone."""
    created = []

    async def fake_create(enable_recording, *, ttl_secs):
        created.append(ttl_secs)
        return _room("fresh")

    monkeypatch.setattr(rp, "create_room_now", fake_create)
    stale = _room("stale", expires_in=rp.BB_DAILY_ROOM_MIN_REMAINING_SECS - 60)
    rp._queue(True).put_nowait(stale)

    got = await rp.acquire_room(True)
    assert got.room_name == "fresh"
    assert _clean == ["stale"], "a stale room must be deleted, not leaked"


async def test_a_stale_room_does_not_hide_a_good_one_behind_it(monkeypatch, _clean):
    async def boom(*a, **k):
        raise AssertionError("fell through despite a usable room in the queue")

    monkeypatch.setattr(rp, "create_room_now", boom)
    rp._queue(True).put_nowait(_room("stale", expires_in=10))
    rp._queue(True).put_nowait(_room("good"))

    got = await rp.acquire_room(True)
    assert got.room_name == "good"
    assert _clean == ["stale"]


def test_a_fresh_room_is_usable_and_a_spent_one_is_not():
    assert _room(expires_in=rp.BB_DAILY_ROOM_MIN_REMAINING_SECS + 1).usable_for_a_call()
    assert not _room(
        expires_in=rp.BB_DAILY_ROOM_MIN_REMAINING_SECS - 1
    ).usable_for_a_call()


# ---------------------------------------------------------------------------
# refilling
# ---------------------------------------------------------------------------


async def test_top_up_fills_to_the_configured_size(monkeypatch):
    async def fake_create(enable_recording, *, ttl_secs):
        return _room(f"r{time.monotonic_ns()}")

    monkeypatch.setattr(rp, "create_room_now", fake_create)
    rp._demand[True].depth = 5
    await rp._top_up(True)
    assert rp.pool_depth(True) == 5


async def test_a_refill_failure_does_not_kill_the_loop(monkeypatch):
    """Daily being down must degrade to inline creation, not stop refilling."""
    attempts = []

    async def flaky(enable_recording, *, ttl_secs):
        attempts.append(1)
        raise RuntimeError("daily is down")

    async def flag_on() -> bool:
        return True

    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL", flag_on)
    monkeypatch.setattr(rp, "create_room_now", flaky)
    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL_REFILL_INTERVAL_SECS", 3600)

    rp._demand[True].depth = 3
    task = asyncio.create_task(rp._refill_loop())
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts, "the loop never tried"


def test_size_zero_disables_the_pool(monkeypatch):
    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL_MAX", 0)
    rp.start_room_pool()
    assert rp._refill_task is None


async def test_shutdown_releases_rooms_nobody_used(monkeypatch, _clean):
    """A pod restarting every deploy would otherwise leave a trail of rooms."""
    rp._queue(True).put_nowait(_room("unused-a"))
    rp._queue(False).put_nowait(_room("unused-b"))
    await rp.stop_room_pool()
    assert sorted(_clean) == ["unused-a", "unused-b"]
    assert rp._pools == {}


# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------


def test_the_pool_covers_the_largest_burst_the_pod_will_admit():
    """The sizing argument in one assertion.

    Erlang-B says three rooms would serve independent arrivals at the peak
    rate, but a burst of k simultaneous requests strands k - N of them at any
    arrival rate. BB_MAX_CONCURRENT_DAILY_BOTS is the most this pod ever
    admits, so a pool at least that deep cannot be outrun — and rooms are
    built concurrently, so depth costs no startup time.
    """
    from app.core.config.static import BB_MAX_CONCURRENT_DAILY_BOTS

    assert rp.BB_DAILY_ROOM_POOL_MAX >= BB_MAX_CONCURRENT_DAILY_BOTS, (
        "a burst up to the capacity gate must never fall through to inline "
        "room creation"
    )


async def test_a_full_capacity_burst_is_served_entirely_from_the_pool(monkeypatch):
    """The end-to-end version: fill to size, take a whole ceiling's worth at
    once, and assert Daily was never called on the request path."""
    from app.core.config.static import BB_MAX_CONCURRENT_DAILY_BOTS

    async def fake_create(enable_recording, *, ttl_secs):
        return _room(f"r{time.monotonic_ns()}")

    monkeypatch.setattr(rp, "create_room_now", fake_create)
    rp._demand[True].depth = BB_MAX_CONCURRENT_DAILY_BOTS
    await rp._top_up(True)

    async def boom(*a, **k):
        raise AssertionError("a caller hit the Daily API during a burst")

    monkeypatch.setattr(rp, "create_room_now", boom)
    rooms = await asyncio.gather(
        *(rp.acquire_room(True) for _ in range(BB_MAX_CONCURRENT_DAILY_BOTS))
    )
    assert (
        len({r.room_name for r in rooms}) == BB_MAX_CONCURRENT_DAILY_BOTS
    ), "two callers were handed the same room"


# ---------------------------------------------------------------------------
# the sizing controller
# ---------------------------------------------------------------------------


def _tick(flavour: bool, takes: int) -> int:
    """One control tick that saw `takes` requests."""
    rp._demand[flavour].takes_this_tick = takes
    if takes:
        rp._demand[flavour].last_take = time.monotonic()
    return rp._advance_controller(flavour, build_secs=rp._BUILD_SECS)


def test_it_starts_at_the_configured_minimum():
    """Nothing is known about traffic yet, so hold the seed depth."""
    assert rp._demand[True].depth == rp.BB_DAILY_ROOM_POOL_MIN


def test_a_burst_raises_depth_immediately(monkeypatch):
    """A room costs nothing to hold and running short costs a caller ~476ms,
    so the pool must not ration itself up one tick at a time."""
    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL_MAX", 20)
    depth = _tick(True, 12)
    assert depth >= 12, f"a burst of 12 left depth at {depth}"


def test_a_burst_keeps_its_headroom_for_a_while(monkeypatch):
    """A rush that just happened will probably happen again, so the peak is
    honoured for _PEAK_TTL_TICKS before the pool starts giving depth back."""
    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL_MAX", 20)
    _tick(True, 12)
    high = rp._demand[True].depth
    held = [_tick(True, 0) for _ in range(rp._PEAK_TTL_TICKS - 1)]
    assert all(d == high for d in held), f"dropped headroom too early: {held}"


def test_depth_is_given_back_one_room_at_a_time(monkeypatch):
    """Shrinking in one jump would strand the next burst."""
    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL_MAX", 20)
    _tick(True, 12)
    high = rp._demand[True].depth
    depths = [_tick(True, 0) for _ in range(rp._PEAK_TTL_TICKS + 6)]
    shrinking = [d for d in depths if d < high]
    assert shrinking, "never came down at all"
    steps = [a - b for a, b in zip([high] + shrinking, shrinking)]
    assert all(s <= 1 for s in steps), f"gave back more than one per tick: {depths}"
    assert depths[-1] >= rp.BB_DAILY_ROOM_POOL_MIN


def test_silence_drains_the_pool_to_zero(monkeypatch):
    """No traffic must cost nothing — that is the whole point of sizing."""
    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL_MAX", 20)
    _tick(True, 5)
    # Last take far enough back to be outside the idle window.
    rp._demand[True].last_take = time.monotonic() - rp.BB_DAILY_ROOM_POOL_IDLE_SECS - 1
    for _ in range(40):
        _tick(True, 0)
    assert rp._demand[True].depth == 0


def test_the_two_flavours_are_sized_independently(monkeypatch):
    """Widget traffic must not provision rooms for recorded calls."""
    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL_MAX", 20)
    _tick(False, 10)
    assert rp._demand[False].depth >= 10
    assert rp._demand[True].depth == rp.BB_DAILY_ROOM_POOL_MIN


def test_depth_never_exceeds_the_capacity_gate(monkeypatch):
    """Past the gate the session is rejected, so deeper cannot help."""
    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL_MAX", 20)
    assert _tick(True, 500) == 20


def test_erlang_term_tracks_sustained_rate(monkeypatch):
    """Steady traffic with no visible bursts should still lift depth above the
    floor once the rate justifies it."""
    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL_MAX", 20)
    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL_MIN", 1)
    rp._demand[True] = rp.Demand(depth=1)
    for _ in range(30):
        _tick(True, 3)
    assert rp._demand[True].depth >= 3


async def test_every_acquire_is_counted(monkeypatch):
    async def fake_create(enable_recording, *, ttl_secs):
        return _room("x")

    monkeypatch.setattr(rp, "create_room_now", fake_create)
    rp._demand[True].takes_this_tick = 0
    for _ in range(4):
        await rp.acquire_room(True)
    assert rp._demand[True].takes_this_tick == 4


# ---------------------------------------------------------------------------
# the kill switch
# ---------------------------------------------------------------------------


async def _one_tick(monkeypatch, *, enabled: bool) -> None:
    """Run exactly one pass of the refill loop with the flag set."""

    async def flag() -> bool:
        return enabled

    async def fake_create(enable_recording, *, ttl_secs):
        return _room(f"r{time.monotonic_ns()}")

    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL", flag)
    monkeypatch.setattr(rp, "create_room_now", fake_create)
    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL_REFILL_INTERVAL_SECS", 3600)

    task = asyncio.create_task(rp._refill_loop())
    for _ in range(50):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_pool_holds_nothing_while_the_flag_is_off(monkeypatch):
    """Ships off: low traffic does not justify creating rooms nobody uses."""
    await _one_tick(monkeypatch, enabled=False)
    assert rp.pool_depth(True) == 0
    assert rp.pool_depth(False) == 0
    assert rp._demand[True].depth == 0


async def test_turning_the_flag_on_fills_the_pool(monkeypatch):
    """Flippable without a deploy, in both directions."""
    await _one_tick(monkeypatch, enabled=True)
    assert rp.pool_depth(True) > 0


async def test_turning_the_flag_off_drains_rooms_already_held(monkeypatch, _clean):
    """Rooms held when the flag flips must be released, not stranded."""
    await _one_tick(monkeypatch, enabled=True)
    held = rp.pool_depth(True) + rp.pool_depth(False)
    assert held > 0
    await _one_tick(monkeypatch, enabled=False)
    assert rp.pool_depth(True) == 0
    assert len(_clean) >= held, "drained rooms were not deleted from Daily"


async def test_a_flag_read_failure_falls_back_to_off(monkeypatch, _clean):
    """Redis trouble must not silently leave the pool running unmanaged."""

    async def boom() -> bool:
        raise RuntimeError("redis down")

    async def fake_create(enable_recording, *, ttl_secs):
        return _room("x")

    rp._queue(True).put_nowait(_room("stranded"))
    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL", boom)
    monkeypatch.setattr(rp, "create_room_now", fake_create)
    monkeypatch.setattr(rp, "BB_DAILY_ROOM_POOL_REFILL_INTERVAL_SECS", 3600)

    task = asyncio.create_task(rp._refill_loop())
    for _ in range(50):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rp.pool_depth(True) == 0


async def test_with_the_pool_off_a_session_still_gets_a_room(monkeypatch):
    """The fallback IS the old behaviour — off must never mean broken."""
    created = []

    async def fake_create(enable_recording, *, ttl_secs):
        created.append(ttl_secs)
        return _room("inline")

    monkeypatch.setattr(rp, "create_room_now", fake_create)
    got = await rp.acquire_room(True)
    assert got.room_name == "inline"
    assert created == [rp.BB_DAILY_ROOM_MIN_REMAINING_SECS]

"""Switching numbers between today's dialler and v2 (design card §6b rules 21, 22, 27;
Fable C1, C3, I4, I8). ``decide`` is pure; the apply steps run on real Redis with the DB
mocked."""

import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch import channel_semaphore as ch_mod
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    reconcile as RC,
    routes,
    routes as R,
    scripts,
    switch as SWT,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2.switch import (
    DRAIN,
    HANDOVER,
    NONE,
    OBSERVE,
    RESEED,
    SEED,
    TURN_ON,
    Facts,
    NumState,
    Step,
    decide,
    is_desired,
)
from app.core.config import dynamic as dyn_cfg
from app.schemas import CallProvider, TelephonyNumber, TelephonyNumberStatus
from app.services.live_config import store as cfg_store
from tests.breeze_buddy.dispatch.v2.conftest import (
    OWNER,
    claim_next,
    seed_number,
    tickets_of,
    use_redis,
)

T0 = 1_000_000


def _number(n="N1", provider=CallProvider.PLIVO, status=None, max_lines=5):
    return TelephonyNumber(
        id=n,
        number=f"+91{n}",
        provider=provider,
        status=status or TelephonyNumberStatus.AVAILABLE,
        channels=0,
        maximum_channels=max_lines,
    )


def _pending(since=T0, count=0, sig=""):
    return NumState(
        mode="v2_pending", mode_since_ms=since, stable_count=count, stable_sig=sig
    )


# -- decide(): every transition -----------------------------------------------------------


def test_off_number_turns_on_when_desired_and_stays_off_otherwise():
    assert decide(NumState(), True, Facts(T0)) == Step(TURN_ON)
    assert decide(NumState(mode="legacy"), True, Facts(T0)) == Step(TURN_ON)
    assert decide(NumState(), False, Facts(T0)) == Step(NONE)


def test_pending_counts_identical_signatures_before_seeding():
    first = decide(_pending(), True, Facts(T0 + 5_000, sig="a"))
    assert first == Step(OBSERVE, stable_count=1, stable_sig="a")
    # same signature twice, but only 10 s in: keep waiting
    second = decide(_pending(count=1, sig="a"), True, Facts(T0 + 10_000, sig="a"))
    assert second == Step(OBSERVE, stable_count=2, stable_sig="a")
    # >= 15 s and two identical checks in a row: seed
    assert decide(
        _pending(count=2, sig="a"), True, Facts(T0 + 15_000, sig="a")
    ) == Step(SEED)


def test_pending_never_seeds_before_15_s_or_on_a_changed_signature():
    assert decide(
        _pending(count=5, sig="a"), True, Facts(T0 + 14_999, sig="a")
    ).action == (OBSERVE)
    changed = decide(_pending(count=5, sig="a"), True, Facts(T0 + 60_000, sig="b"))
    assert changed == Step(OBSERVE, stable_count=1, stable_sig="b")
    unreadable = decide(_pending(count=5, sig="a"), True, Facts(T0 + 60_000, sig=None))
    assert unreadable == Step(OBSERVE)  # start counting again


def test_pending_not_desired_hands_back_at_once():
    assert decide(_pending(), False, Facts(T0, drained=True)) == Step(HANDOVER)
    # a leftover ticket still out: drain it first
    assert decide(_pending(), False, Facts(T0, drained=False)) == Step(DRAIN)


def test_v2_reseeds_once_then_rests_and_drains_when_not_desired():
    v2 = NumState(mode="v2", reseed_at_ms=T0 + 20_000)
    assert decide(v2, True, Facts(T0 + 19_999)) == Step(NONE)
    assert decide(v2, True, Facts(T0 + 20_000)) == Step(RESEED)
    assert decide(NumState(mode="v2"), True, Facts(T0 + 99_000)) == Step(NONE)
    assert decide(v2, False, Facts(T0)) == Step(DRAIN)


def test_an_unfinished_hand_back_is_finished_before_anything_else():
    pending = NumState(mode="legacy", handback_pending=True)
    assert decide(pending, True, Facts(T0)) == Step(HANDOVER)
    assert decide(pending, False, Facts(T0)) == Step(HANDOVER)
    assert NumState.from_hash({"mode": "legacy", "handback_pending": "1"}) == pending


def test_draining_waits_for_tickets_then_hands_back_even_if_desired_again():
    draining = NumState(mode="draining")
    assert decide(draining, False, Facts(T0, drained=False)) == Step(NONE)
    assert decide(draining, False, Facts(T0, drained=True)) == Step(HANDOVER)
    # flapping: desired again mid-drain -> finish the drain first ...
    assert decide(draining, True, Facts(T0, drained=False)) == Step(NONE)
    assert decide(draining, True, Facts(T0, drained=True)) == Step(HANDOVER)
    # ... then switch on from legacy
    assert decide(NumState(mode="legacy"), True, Facts(T0)) == Step(TURN_ON)


def test_desired_set_is_flag_and_list_and_available_plivo():
    on = ["N1"]
    assert is_desired(_number(), True, on)
    # Vobiz dials through the same path as Plivo in this release
    assert is_desired(_number(provider=CallProvider.VOBIZ), True, on)
    # Twilio gates on the number's status, not a line count: today's path
    assert not is_desired(_number(provider=CallProvider.TWILIO), True, on)
    assert not is_desired(_number(), False, on)  # flag off: global off
    assert not is_desired(_number(), True, [])  # not listed
    assert not is_desired(None, True, on)  # no such number
    assert not is_desired(_number(provider=CallProvider.TWILIO), True, on)  # never
    assert not is_desired(_number(provider=CallProvider.EXOTEL), True, on)
    for status in (TelephonyNumberStatus.DISABLED, TelephonyNumberStatus.IN_USE):
        assert not is_desired(_number(status=status), True, on)  # rule 27 / I4


# -- apply steps on real Redis ------------------------------------------------------------


@pytest.fixture
async def rv(rr, monkeypatch):
    use_redis(monkeypatch, rr, SWT, RC, routes, ch_mod)
    yield rr


@pytest.fixture
def db(monkeypatch):
    """The DB as switch.py and the seeding helpers see it."""
    d = NS(
        numbers={"N1": _number()},
        live=[],  # (lead_id, direction, call_id) on N1
        locked=[],  # (lead_id, template_id, is_locked)
        processing={},  # {number_id: PROCESSING count}
        channels=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        SWT,
        "get_telephony_numbers_by_ids",
        AsyncMock(
            side_effect=lambda ids: {i: d.numbers[i] for i in ids if i in d.numbers}
        ),
    )
    monkeypatch.setattr(
        SWT,
        "count_processing_by_telephony_number",
        AsyncMock(side_effect=lambda: d.processing),
    )
    monkeypatch.setattr(SWT, "set_telephony_number_channels", d.channels)
    monkeypatch.setattr(
        RC, "get_live_calls_on_number", AsyncMock(side_effect=lambda n: d.live)
    )
    monkeypatch.setattr(
        RC, "get_legacy_inflight_leads", AsyncMock(side_effect=lambda ids: d.locked)
    )
    return d


def _config(monkeypatch, enabled=True, numbers=("N1",)):
    monkeypatch.setattr(
        SWT.dyn_cfg, "BB_DISPATCH_V2_ENABLED", AsyncMock(return_value=enabled)
    )
    monkeypatch.setattr(
        SWT.dyn_cfg, "BB_DISPATCH_V2_NUMBERS", AsyncMock(return_value=list(numbers))
    )


async def test_a_listed_twilio_number_stays_on_todays_path_with_one_warning(
    rv, db, monkeypatch
):
    _config(monkeypatch, numbers=("V1",))
    db.numbers["V1"] = _number("V1", provider=CallProvider.TWILIO)
    monkeypatch.setattr(SWT, "_warned_unsupported", set())
    warn = []
    monkeypatch.setattr(SWT.logger, "warning", lambda m, *a, **k: warn.append(m))
    await SWT.run_switch_step()
    await SWT.run_switch_step()
    assert await rv.hget("bb:num:V1", "mode") is None  # never turned on
    assert len([m for m in warn if "V1" in m and "Plivo and Vobiz only" in m]) == 1


async def test_turn_on_writes_facts_mode_and_active_set(rv):
    await SWT.apply_turn_on(rv, _number(max_lines=7), T0)
    num = await rv.hgetall("bb:num:N1")
    assert num["mode"] == "v2_pending" and num["mode_since_ms"] == str(T0)
    assert num["max"] == "7" and num["provider"] == "PLIVO"
    assert await rv.smembers("bb:v2:active") == {"N1"}


async def test_seed_fills_busy_from_live_calls_then_issues_only_free_lines(
    rv, db, monkeypatch
):
    monkeypatch.setattr(
        RC, "invalidate_route", AsyncMock()
    )  # the route below is the truth
    await seed_number(rv, "N1", 5, {"T1": {}}, mode="v2_pending")
    now = int(time.time() * 1000)
    await rv.zadd("bb:q:T1", {"W1": now - 3, "W2": now - 2, "W3": now - 1})
    db.live = [
        ("P1", "OUTBOUND", "c1"),
        ("P2", "OUTBOUND", "c2"),
        ("I1", "INBOUND", "C9"),
    ]
    db.locked = [("K1", "T1", True)]
    await SWT.apply_seed(rv, "N1", T0)
    busy = await rv.smembers("bb:busy:N1")
    assert {"lead:P1", "lead:P2", "call:C9", "lead:K1"} <= busy
    assert await tickets_of(rv, "N1") == ["W1"]  # 5 lines - 4 held = 1
    num = await rv.hgetall("bb:num:N1")
    assert num["mode"] == "v2" and num["reseed_at_ms"] == str(T0 + 20_000)


async def test_seed_aborts_without_a_mode_change_when_the_db_fails(rv, db, monkeypatch):
    await seed_number(rv, "N1", 5, {"T1": {}}, mode="v2_pending")
    monkeypatch.setattr(
        RC, "get_live_calls_on_number", AsyncMock(side_effect=RuntimeError("db"))
    )
    with pytest.raises(RuntimeError):
        await SWT.apply_seed(rv, "N1", T0)
    assert await rv.hget("bb:num:N1", "mode") == "v2_pending"


async def test_reseed_only_adds(rv, db):
    await seed_number(rv, "N1", 5, {"T1": {}})
    await rv.hset("bb:num:N1", "reseed_at_ms", T0)
    await rv.sadd("bb:busy:N1", "lead:OLD")
    db.live = [("P9", "OUTBOUND", "c")]
    await SWT.apply_reseed(rv, "N1")
    assert await rv.smembers("bb:busy:N1") == {"lead:OLD", "lead:P9"}
    assert await rv.hget("bb:num:N1", "reseed_at_ms") is None


async def test_handover_order_and_values(rv, db, monkeypatch):
    await seed_number(rv, "N1", 5, {"T1": {}, "T2": {}}, mode="draining")
    await rv.sadd("bb:v2:active", "N1", "N2")
    await rv.zadd("bb:due", {"N1": 1})
    await rv.sadd("bb:busy:N1", "lead:A", "lead:B")
    await rv.zadd("bb:q:T1", {"L1": 111, "L2": 222})
    await rv.zadd("bb:q:T2", {"L3": 333})
    await rv.rpush("bb:channel:N1", "stale-token")
    db.processing = {"N1": 3, "N9": 1}
    calls = []
    real_move = scripts.move_room_to_schedule

    async def move(t):
        calls.append(("move", t, await rv.hget("bb:num:N1", "mode")))
        return await real_move(t)

    async def count():
        calls.append(("count", await rv.hget("bb:num:N1", "mode")))
        return db.processing

    async def channels(n, value):
        calls.append(("channels", n, value))
        return True

    def no_scan(*a, **k):
        raise AssertionError("SCAN in a hand-back")

    monkeypatch.setattr(SWT.scripts, "move_room_to_schedule", move)
    monkeypatch.setattr(SWT, "count_processing_by_telephony_number", count)
    monkeypatch.setattr(rv, "scan_iter", no_scan)
    db.channels.side_effect = channels
    await SWT.apply_handover(rv, "N1", _number(max_lines=5), T0)

    # 1. rooms to today's schedule while v2 still owns the number ...
    assert calls[:2] == [("move", "T1", "draining"), ("move", "T2", "draining")]
    # 2. ... legacy, and only then the DB's PROCESSING count (never SCARD) ...
    assert calls[2:4] == [("count", "legacy"), ("channels", "N1", 3)]
    # 3. ... today's tokens = max - count ...
    assert await rv.llen("bb:channel:N1") == 2
    # 4. ... and the rooms listed again after the flip
    assert [c[1] for c in calls[4:]] == ["T1", "T2"]
    assert await rv.zrange("bb:schedule:leads", 0, -1, withscores=True) == [
        ("L1", 111.0),
        ("L2", 222.0),
        ("L3", 333.0),
    ]
    assert not await rv.exists("bb:q:T1") and not await rv.exists("bb:q:T2")
    num = await rv.hgetall("bb:num:N1")
    assert num["mode"] == "legacy" and "handback_pending" not in num
    assert await rv.smembers("bb:v2:active") == {"N2"}
    assert await rv.zscore("bb:due", "N1") is None
    for key in ("bb:busy:N1", "bb:inflight:N1"):
        assert not await rv.exists(key)


async def test_handover_with_every_line_taken_leaves_no_tokens(rv, db):
    await seed_number(rv, "N1", 2, {"T1": {}}, mode="draining")
    await rv.rpush("bb:channel:N1", "t1", "t2")
    db.processing = {"N1": 2}
    await SWT.apply_handover(rv, "N1", _number(max_lines=2), T0)
    assert await rv.llen("bb:channel:N1") == 0


async def test_handover_moves_a_lead_enqueued_while_it_ran(rv, db, monkeypatch):
    # enqueue lands in the room after the first move but before the flip to legacy
    await seed_number(rv, "N1", 2, {"T1": {}}, mode="draining")
    real_move = scripts.move_room_to_schedule
    raced = []

    async def move(t):
        moved = await real_move(t)
        if not raced:
            raced.append(1)
            assert await scripts.enqueue("T1", "LATE", 444) == 0
        return moved

    monkeypatch.setattr(SWT.scripts, "move_room_to_schedule", move)
    await SWT.apply_handover(rv, "N1", _number(max_lines=2), T0)
    assert await rv.zscore("bb:schedule:leads", "LATE") == 444
    assert not await rv.exists("bb:q:T1")


async def test_handover_of_a_disabled_number_builds_no_tokens(rv, db):
    # like today's reconciler, which keeps no channel state for a DISABLED number
    await seed_number(rv, "N1", 4, {"T1": {}}, mode="draining")
    await rv.rpush("bb:channel:N1", "old")
    db.processing = {"N1": 0}
    disabled = _number(status=TelephonyNumberStatus.DISABLED, max_lines=4)
    await SWT.apply_handover(rv, "N1", disabled, T0)
    assert await rv.lrange("bb:channel:N1", 0, -1) == ["old"]
    db.channels.assert_awaited_once_with("N1", 0)
    assert await rv.hget("bb:num:N1", "mode") == "legacy"


async def test_a_failed_hand_back_is_marked_and_finished_on_the_next_check(
    rv, db, monkeypatch
):
    _config(monkeypatch, enabled=False)
    await seed_number(rv, "N1", 2, {"T1": {}}, mode="draining")
    await rv.sadd("bb:v2:active", "N1")
    await rv.zadd("bb:q:T1", {"L1": 1})
    db.numbers["N1"] = _number(max_lines=2)
    db.processing = {"N1": 1}
    db.channels.side_effect = RuntimeError("db down")
    await SWT.run_switch_step()  # fails after the flip; logged, retried next check
    num = await rv.hgetall("bb:num:N1")
    assert num["mode"] == "legacy" and num["handback_pending"] == "1"
    assert await rv.smembers("bb:v2:active") == {"N1"}  # still checked
    db.channels.side_effect = None
    _config(monkeypatch, enabled=True)  # desired again: the hand-back finishes first
    await SWT.run_switch_step()
    num = await rv.hgetall("bb:num:N1")
    assert num["mode"] == "legacy" and "handback_pending" not in num
    db.channels.assert_awaited_with("N1", 1)
    assert await rv.llen("bb:channel:N1") == 1
    assert await rv.scard("bb:v2:active") == 0
    assert await rv.zscore("bb:schedule:leads", "L1") == 1
    await SWT.run_switch_step()
    assert await rv.hget("bb:num:N1", "mode") == "v2_pending"  # ... then on


async def test_strict_config_read_raises_where_get_config_falls_back(monkeypatch):
    # ruling C-concern 4: the switch must tell "read failed" from "off"
    monkeypatch.setattr(cfg_store, "ENABLE_REDIS_DYNAMIC_CONFIG", True)
    monkeypatch.delenv("BB_DISPATCH_V2_ENABLED", raising=False)
    monkeypatch.setattr(
        cfg_store, "get_redis_service", AsyncMock(side_effect=ConnectionError("blip"))
    )
    assert await dyn_cfg.BB_DISPATCH_V2_ENABLED() is False  # today's callers: default
    with pytest.raises(ConnectionError):
        await dyn_cfg.BB_DISPATCH_V2_ENABLED(strict=True)
    with pytest.raises(ConnectionError):
        await dyn_cfg.BB_DISPATCH_V2_NUMBERS(strict=True)


async def test_the_switch_reads_its_config_strictly(rv, db, monkeypatch):
    await seed_number(rv, "N1", 2, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    enabled = AsyncMock(side_effect=ConnectionError("blip"))
    monkeypatch.setattr(SWT.dyn_cfg, "BB_DISPATCH_V2_ENABLED", enabled)
    with pytest.raises(ConnectionError):
        await SWT.run_switch_step()
    enabled.assert_awaited_once_with(strict=True)
    assert await rv.hget("bb:num:N1", "mode") == "v2"


# -- the switch job -----------------------------------------------------------------------


async def test_global_off_drains_every_v2_number_in_one_step(rv, db, monkeypatch):
    _config(monkeypatch, enabled=False)
    for n in ("N1", "N2", "N3"):
        await seed_number(rv, n, 2, {f"T{n}": {}})
        await rv.sadd("bb:v2:active", n)
        lease = {"t": f"T{n}", "tk": 1, "issued_ms": 0}
        await rv.hset(f"bb:inflight:{n}", "ticket", json.dumps(lease))  # still out
        db.numbers[n] = _number(n)
    await SWT.run_switch_step()
    for n in ("N1", "N2", "N3"):
        assert await rv.hget(f"bb:num:{n}", "mode") == "draining"


async def test_a_number_that_stops_being_available_drains_to_todays_path(
    rv, db, monkeypatch
):
    _config(monkeypatch)
    await seed_number(rv, "N1", 2, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    db.numbers["N1"] = _number(status=TelephonyNumberStatus.DISABLED)
    await SWT.run_switch_step()
    assert await rv.hget("bb:num:N1", "mode") == "draining"
    db.processing = {}
    await SWT.run_switch_step()  # nothing in flight: handed back
    assert await rv.hget("bb:num:N1", "mode") == "legacy"


async def test_twilio_is_never_switched_on(rv, db, monkeypatch):
    _config(monkeypatch)
    db.numbers["N1"] = _number(provider=CallProvider.TWILIO)
    await SWT.run_switch_step()
    assert await rv.hget("bb:num:N1", "mode") is None
    assert await rv.scard("bb:v2:active") == 0


async def test_a_db_error_changes_nothing(rv, db, monkeypatch):
    _config(monkeypatch, enabled=False)
    await seed_number(rv, "N1", 2, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    monkeypatch.setattr(
        SWT, "get_telephony_numbers_by_ids", AsyncMock(side_effect=RuntimeError("db"))
    )
    with pytest.raises(RuntimeError):
        await SWT.run_switch_step()
    assert await rv.hget("bb:num:N1", "mode") == "v2"


async def test_one_failing_number_does_not_stop_the_others(rv, db, monkeypatch):
    _config(monkeypatch, numbers=("N1", "N2"))
    db.numbers["N2"] = _number("N2")
    real = SWT.apply_turn_on

    async def turn_on(c, number, now_ms, *a, **k):  # the step also gets what decide saw
        if number.id == "N1":
            raise RuntimeError("boom")
        await real(c, number, now_ms, *a, **k)

    monkeypatch.setattr(SWT, "apply_turn_on", turn_on)
    await SWT.run_switch_step()
    assert await rv.hget("bb:num:N2", "mode") == "v2_pending"


@pytest.mark.parametrize("first_use", [False, True])
async def test_recover_after_flush_restarts_desired_numbers_at_pending(
    rv, db, monkeypatch, first_use
):
    _config(monkeypatch, numbers=("N1", "N2"))
    db.numbers["N2"] = _number("N2", provider=CallProvider.TWILIO)
    monkeypatch.setattr(SWT, "_now_ms", lambda: T0)
    assert await SWT.recover_after_flush(first_use=first_use) is True
    assert await rv.hget("bb:num:N1", "mode") == "v2_pending"
    assert await rv.hget("bb:num:N1", "max") == "5"
    assert await rv.smembers("bb:v2:active") == {"N1"}
    assert await rv.get("bb:epoch") == str(T0)
    db.channels.assert_not_awaited()  # no legacy recount on this path


# -- Redis lost, the v2 flags with it (fix 1-B, x7b) --------------------------------------


def _fleet(db, monkeypatch):
    """Every number in the DB, with stale token lists, and the DB's PROCESSING counts."""
    db.numbers = {
        "P": _number("P", max_lines=5),
        "V": _number("V", provider=CallProvider.VOBIZ, max_lines=3),
        "T": _number("T", provider=CallProvider.TWILIO, max_lines=4),
        "D": _number("D", status=TelephonyNumberStatus.DISABLED, max_lines=2),
    }
    db.processing = {"P": 2, "V": 3, "T": 1}
    monkeypatch.setattr(
        SWT,
        "list_telephony_numbers",
        AsyncMock(side_effect=lambda: list(db.numbers.values())),
    )


async def _stale_tokens(rv):
    for n in ("P", "V", "T", "D"):
        await rv.rpush(f"bb:channel:{n}", "stale")


def _channels_written(db):
    return sorted(tuple(call.args) for call in db.channels.await_args_list)


FLAGS_GONE = [
    {"enabled": False},  # the flag fell back to its default
    {"enabled": True, "numbers": ()},  # the list did
]


def test_the_recovery_wait_covers_the_slowest_providers_request_timeout():
    """A dial in flight at the loss must have settled before the recount: the wait is at
    least Plivo's and Vobiz's request timeouts (Vobiz's 30 s is the longer)."""
    from app.ai.voice.agents.breeze_buddy.services.telephony.vobiz import vobiz
    from app.core.config.static import PLIVO_REST_TIMEOUT_SECONDS

    slowest_s = max(PLIVO_REST_TIMEOUT_SECONDS, vobiz._REQUEST_TIMEOUT_SECONDS)
    assert SWT.LEGACY_RECOVERY_WAIT_MS >= slowest_s * 1000
    # and today's greeting pre-warm before it (up to 60.5 s): Swaroop's #1314 review
    assert SWT.LEGACY_RECOVERY_WAIT_MS >= (60.5 + slowest_s) * 1000
    assert SWT.LEGACY_RECOVERY_WAIT_MS == 95_000


@pytest.mark.parametrize("config", FLAGS_GONE)
async def test_flags_gone_recovery_writes_nothing_before_30_s(
    rv, db, monkeypatch, config
):
    """Redis lost v2's keys and the flags too, so no number goes back to v2 and every one is
    on today's path. Today's workers hold (bb:epoch missing) while a dial in flight at the
    loss settles: nothing is recounted, and the epoch stays missing, for 30 s."""
    _config(monkeypatch, **config)
    _fleet(db, monkeypatch)
    await _stale_tokens(rv)
    assert await SWT.recover_after_flush(lost_for_ms=0) is False
    assert (
        await SWT.recover_after_flush(lost_for_ms=15_000) is False
    )  # Plivo's: too soon
    assert (
        await SWT.recover_after_flush(lost_for_ms=SWT.LEGACY_RECOVERY_WAIT_MS - 1)
        is False
    )
    assert not await rv.exists("bb:epoch")
    db.channels.assert_not_awaited()
    for n in ("P", "V", "T", "D"):
        assert await rv.lrange(f"bb:channel:{n}", 0, -1) == ["stale"]


@pytest.mark.parametrize("config", FLAGS_GONE)
async def test_flags_gone_recovery_after_30_s_rebuilds_todays_counters_from_the_db(
    rv, db, monkeypatch, config
):
    """After 30 s, every Plivo and Vobiz number gets the hand-back's counters: DB channels =
    the DB's own PROCESSING count, today's tokens = max - that count (none for a DISABLED
    number, like today's reconciler); the same for Vobiz. Twilio, never on v2, is untouched. Then the epoch is set, marked as
    a legacy recovery, and the hold ends; no number is switched to v2."""
    _config(monkeypatch, **config)
    _fleet(db, monkeypatch)
    await _stale_tokens(rv)
    monkeypatch.setattr(SWT, "_now_ms", lambda: T0)
    assert (
        await SWT.recover_after_flush(lost_for_ms=SWT.LEGACY_RECOVERY_WAIT_MS) is True
    )
    assert _channels_written(db) == [("D", 0), ("P", 2), ("V", 3)]
    assert await rv.llen("bb:channel:P") == 3  # 5 lines - 2 calls
    assert await rv.llen("bb:channel:V") == 0  # 3 lines - 3 calls
    assert await rv.lrange("bb:channel:D", 0, -1) == ["stale"]  # DISABLED: no tokens
    assert await rv.lrange("bb:channel:T", 0, -1) == ["stale"]  # Twilio: untouched
    assert await rv.get("bb:epoch") == f"legacy-recovery:{T0}"
    assert await rv.scard("bb:v2:active") == 0
    assert await rv.hget("bb:num:P", "mode") is None


@pytest.mark.parametrize("config", FLAGS_GONE)
async def test_first_use_with_no_desired_number_just_sets_the_epoch(
    rv, db, monkeypatch, config
):
    """v2 turned on with no number listed (or none AVAILABLE): the epoch was never set, so
    nothing was lost and v2 never ran. The epoch is set at once, as a normal one: no wait,
    no recount of today's counters."""
    _config(monkeypatch, **config)
    _fleet(db, monkeypatch)
    await _stale_tokens(rv)
    monkeypatch.setattr(SWT, "_now_ms", lambda: T0)
    assert await SWT.recover_after_flush(lost_for_ms=0, first_use=True) is True
    assert await rv.get("bb:epoch") == str(T0)
    db.channels.assert_not_awaited()
    for n in ("P", "V", "T", "D"):
        assert await rv.lrange(f"bb:channel:{n}", 0, -1) == ["stale"]


async def test_first_enable_with_no_number_listed_holds_nothing_and_sets_the_epoch(
    rv, db, monkeypatch
):
    """End to end: BB_DISPATCH_V2_ENABLED first turned on with an empty list. Today's
    worker is not held while the epoch is missing, and the leader's first tick sets it.
    """
    from app.ai.voice.agents.breeze_buddy.dispatch import queue as queue_mod
    from app.ai.voice.agents.breeze_buddy.dispatch.v2 import sweep as SW

    _config(monkeypatch, enabled=True, numbers=())
    _fleet(db, monkeypatch)
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(SW, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(SW, "JOBS", ())
    monkeypatch.setattr(SWT, "_now_ms", lambda: T0)
    assert await queue_mod.v2_owns_number("P") is False  # dialled as today
    await SW.Sweeper(redis_client=rv).tick()
    assert await rv.get("bb:epoch") == str(T0)
    db.channels.assert_not_awaited()
    assert await queue_mod.v2_owns_number("P") is False


async def test_flags_gone_recovery_waits_while_the_numbers_are_unreadable(
    rv, db, monkeypatch
):
    """A DB error must not read as "no numbers": the hold goes on and the next tick
    retries."""
    _config(monkeypatch, enabled=False)
    _fleet(db, monkeypatch)
    monkeypatch.setattr(
        SWT, "list_telephony_numbers", AsyncMock(side_effect=RuntimeError("db"))
    )
    with pytest.raises(RuntimeError):
        await SWT.recover_after_flush(lost_for_ms=SWT.LEGACY_RECOVERY_WAIT_MS)
    assert not await rv.exists("bb:epoch")


async def test_a_new_leader_repeats_an_unfinished_recovery(rv, db, monkeypatch):
    """The leader dies half-way through the recount: the epoch is still missing, so every
    pod keeps holding. The next leader waits its own 30 s and recounts every number again
    (the same writes: idempotent), then sets the epoch."""
    from app.ai.voice.agents.breeze_buddy.dispatch.v2 import sweep as SW

    _config(monkeypatch, enabled=False)
    _fleet(db, monkeypatch)
    monkeypatch.setattr(SW, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(SW, "JOBS", ())
    clock = [100.0]
    monkeypatch.setattr(SW, "time", NS(monotonic=lambda: clock[0], time=time.time))

    first = SW.Sweeper(redis_client=rv)
    await rv.set("bb:epoch", "x")
    await first.tick()  # the epoch is seen set ...
    await rv.delete("bb:epoch")  # ... then lost
    await first.tick()  # the loss is seen
    clock[0] = 195.0
    db.channels.side_effect = [True, RuntimeError("leader died")]
    with pytest.raises(RuntimeError):
        await first.tick()
    assert db.channels.await_count == 2 and not await rv.exists("bb:epoch")

    db.channels.reset_mock(side_effect=True)
    db.channels.return_value = True
    second = SW.Sweeper(redis_client=rv)  # took the lead, knows nothing of the first
    await second.tick()
    clock[0] = 289.9
    await second.tick()
    db.channels.assert_not_awaited()  # its own 30 s
    assert not await rv.exists("bb:epoch")
    clock[0] = 290.0
    await second.tick()
    assert _channels_written(db) == [("D", 0), ("P", 2), ("V", 3)]
    assert await rv.llen("bb:channel:P") == 3
    assert (await rv.get("bb:epoch")).startswith("legacy-recovery:")


# -- end to end: on with live legacy calls, then off --------------------------------------


async def test_switch_on_with_live_legacy_calls_then_off(rv, db, monkeypatch):
    """Legacy number with live legacy calls -> on -> no ticket before seeding -> seeded busy
    covers the live calls -> v2 issues only the remaining free lines -> off -> hand-back
    counts from the DB."""
    monkeypatch.setattr(RC, "invalidate_route", AsyncMock())  # the routes are seeded
    clock = [T0]
    monkeypatch.setattr(SWT, "_now_ms", lambda: clock[0])
    _config(monkeypatch)
    # N1 (5 lines) on today's path: 2 outbound + 1 inbound live, 1 legacy dispatch running
    await rv.hset(
        "bb:num:N1", mapping={"max": 5, "provider": "PLIVO", "status": "AVAILABLE"}
    )
    await rv.hset(
        "bb:route:T1", mapping={"number": "N1", "enabled": "1", "tier": "normal"}
    )
    await rv.sadd("bb:numtpl:N1", "T1")
    db.live = [
        ("P1", "OUTBOUND", "c1"),
        ("P2", "OUTBOUND", "c2"),
        ("I1", "INBOUND", "C1"),
    ]
    db.locked = [("K1", "T1", True)]

    await SWT.run_switch_step()  # on
    assert await rv.hget("bb:num:N1", "mode") == "v2_pending"
    now = int(time.time() * 1000)
    for i, lead in enumerate(("W1", "W2", "W3")):
        assert await scripts.enqueue("T1", lead, now - 10 + i) == 0  # room, no ticket

    for t in (5_000, 10_000):
        clock[0] = T0 + t
        await SWT.run_switch_step()
        assert len(await tickets_of(rv, "N1")) == 0  # nothing issued before seeding
        assert await rv.hget("bb:num:N1", "mode") == "v2_pending"
    clock[0] = T0 + 15_000
    await SWT.run_switch_step()  # stable for two checks and >= 15 s: seed

    busy = await rv.smembers("bb:busy:N1")
    assert {"lead:P1", "lead:P2", "call:C1", "lead:K1"} <= busy
    assert await rv.scard("bb:busy:N1") == 5  # never more than max
    assert await tickets_of(rv, "N1") == ["W1"]  # only the 1 free line

    # off: no new tickets; the ticket already out is dialled, then the hand-back
    _config(monkeypatch, enabled=False)
    clock[0] = T0 + 20_000
    await SWT.run_switch_step()
    assert await rv.hget("bb:num:N1", "mode") == "draining"
    ticket = await claim_next("N1")
    assert ticket is not None and ticket[0] == "W1"
    # dialled: W1's call holds the line
    assert await scripts.clear_lease("N1", "W1", ticket[1], OWNER)
    assert await scripts.release("N1", "lead:P1") == [1, 0]  # a legacy call ended
    # P2 ended too, but its release was lost: busy still lists it (SCARD 4)
    db.processing = {"N1": 3}  # I1, K1 (now dialled) and W1
    clock[0] = T0 + 25_000
    await SWT.run_switch_step()
    assert await rv.hget("bb:num:N1", "mode") == "legacy"
    db.channels.assert_awaited_with("N1", 3)  # the DB's count, not SCARD (Fable C3)
    assert await rv.llen("bb:channel:N1") == 2  # 5 - 3
    assert [
        m for m, _ in await rv.zrange("bb:schedule:leads", 0, -1, withscores=True)
    ] == [
        "W2",
        "W3",
    ]
    assert await rv.scard("bb:v2:active") == 0
    assert not await rv.exists("bb:busy:N1")
    # a v2-dialled call ending from now on releases through today's path
    assert await routes.number_mode_or_none("N1") == "legacy"


# -- switch-on reads: once per step, locked leads only (Fable I2, I4) --------------------


def _pending_numbers(monkeypatch, db, rv, numbers):
    _config(monkeypatch, numbers=numbers)
    for n in numbers:
        db.numbers[n] = _number(n, max_lines=3)


async def test_switch_on_reads_locked_leads_once_per_step_for_every_number(
    rv, db, monkeypatch
):
    """Fable I2: three numbers switching on together. Each check reads the locked BACKLOG
    leads once and re-resolves each of their templates once, for all numbers (the seed
    reuses the same read), and never scans the keyspace or reads today's lists."""
    clock = [T0]
    monkeypatch.setattr(SWT, "_now_ms", lambda: clock[0])
    _pending_numbers(monkeypatch, db, rv, ("N1", "N2", "N3"))
    for n in ("N1", "N2", "N3"):
        await rv.hset(f"bb:route:T{n}", mapping={"number": n, "enabled": "1"})
    db.locked = [("K1", "TN1", True), ("K2", "TN1", True), ("K3", "TN2", True)]
    reads = AsyncMock(side_effect=lambda ids: db.locked)
    monkeypatch.setattr(RC, "get_legacy_inflight_leads", reads)
    invalidate = AsyncMock()
    monkeypatch.setattr(RC, "invalidate_route", invalidate)
    await rv.rpush("bb:ready:leads", "R1")
    await rv.rpush("bb:processing:leads:w-1", "P1")
    monkeypatch.setattr(rv, "scan", AsyncMock(side_effect=AssertionError("SCAN")))
    monkeypatch.setattr(rv, "scan_iter", AsyncMock(side_effect=AssertionError("SCAN")))
    listed = []
    real_lrange = rv.lrange

    async def lrange(key, *a):
        listed.append(key)
        return await real_lrange(key, *a)

    monkeypatch.setattr(rv, "lrange", lrange)

    await SWT.run_switch_step()  # all three on
    for step_no, t in enumerate((5_000, 10_000, 15_000), start=1):
        clock[0] = T0 + t
        await SWT.run_switch_step()
        assert reads.await_count == step_no  # once per check, not once per number
        assert invalidate.await_count == 2 * step_no  # TN1 and TN2, once each
    assert [await rv.hget(f"bb:num:{n}", "mode") for n in ("N1", "N2", "N3")] == [
        "v2"
    ] * 3
    assert await rv.smembers("bb:busy:N1") == {"lead:K1", "lead:K2"}
    assert await rv.smembers("bb:busy:N2") == {"lead:K3"}
    assert await rv.scard("bb:busy:N3") == 0
    assert listed == []  # today's ready and processing lists are never read


async def test_switch_on_is_not_held_back_by_leads_passing_through_todays_ready_list(
    rv, db, monkeypatch
):
    """Fable I4: only a locked lead can be mid-dial past the redirect; an unlocked lead a
    worker picks from today's ready list is bounced to its room. So the signature is the
    locked set alone, and a steady trickle of due leads through today's lists does not keep
    the number in v2_pending."""
    clock = [T0]
    monkeypatch.setattr(SWT, "_now_ms", lambda: clock[0])
    _pending_numbers(monkeypatch, db, rv, ("N1",))
    await rv.hset("bb:route:T1", mapping={"number": "N1", "enabled": "1"})
    db.locked = [("K1", "T1", True)]
    # BACKLOG rows for any ids passed in, like the query: picked leads are unlocked
    monkeypatch.setattr(
        RC,
        "get_legacy_inflight_leads",
        AsyncMock(side_effect=lambda ids: db.locked + [(i, "T1", False) for i in ids]),
    )
    monkeypatch.setattr(RC, "invalidate_route", AsyncMock())

    await SWT.run_switch_step()  # on
    for t, picked in ((5_000, "R1"), (10_000, "R2"), (15_000, "R3")):
        await rv.delete("bb:ready:leads")
        await rv.rpush("bb:ready:leads", picked)  # a new due lead every check
        clock[0] = T0 + t
        await SWT.run_switch_step()
    assert await rv.hget("bb:num:N1", "mode") == "v2"
    assert await rv.smembers("bb:busy:N1") == {"lead:K1"}


async def test_a_change_in_the_locked_set_still_restarts_the_count(rv, db, monkeypatch):
    """The two-check rule stays: a legacy dispatch that starts or ends between two checks
    means the next seed would be a guess, so pending goes on until two agree."""
    clock = [T0]
    monkeypatch.setattr(SWT, "_now_ms", lambda: clock[0])
    _pending_numbers(monkeypatch, db, rv, ("N1",))
    await rv.hset("bb:route:T1", mapping={"number": "N1", "enabled": "1"})
    monkeypatch.setattr(RC, "invalidate_route", AsyncMock())
    await SWT.run_switch_step()  # on
    for t, locked in ((5_000, "K1"), (10_000, "K1"), (15_000, "K2")):
        db.locked = [(locked, "T1", True)]
        clock[0] = T0 + t
        await SWT.run_switch_step()
    assert (
        await rv.hget("bb:num:N1", "mode") == "v2_pending"
    )  # K1 -> K2: count restarts
    clock[0] = T0 + 20_000
    await SWT.run_switch_step()
    assert await rv.hget("bb:num:N1", "mode") == "v2"
    assert await rv.smembers("bb:busy:N1") == {"lead:K2"}


async def test_unreadable_locked_leads_restart_the_count_and_are_read_once(
    rv, db, monkeypatch
):
    """A failed read of the locked leads is "can't tell" for every pending number in the
    step (the count starts again, nothing is seeded) and is tried once, not per number.
    """
    _pending_numbers(monkeypatch, db, rv, ("N1", "N2"))
    for n in ("N1", "N2"):
        await seed_number(rv, n, 2, {f"T{n}": {}}, mode="v2_pending")
        await rv.sadd("bb:v2:active", n)
        sig = RC.locked_signature(set())
        await rv.hset(
            f"bb:num:{n}",
            mapping={"mode_since_ms": T0, "stable_count": 1, "stable_sig": sig},
        )
    reads = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(RC, "get_legacy_inflight_leads", reads)
    monkeypatch.setattr(SWT, "_now_ms", lambda: T0 + 15_000)
    await SWT.run_switch_step()
    assert reads.await_count == 1
    for n in ("N1", "N2"):
        num = await rv.hgetall(f"bb:num:{n}")
        assert num["mode"] == "v2_pending" and num["stable_count"] == "0"
        assert await rv.scard(f"bb:busy:{n}") == 0


# -- a stale step changes nothing (review #1287 finding 3) ---------------------------------
# Every apply step writes only if the number's mode and mode_since_ms are still the ones the
# step was decided on. The case found in review: leader A decides "seed" for a pending
# number, its DB read is slow, its lock expires; leader B drains and hands the number back;
# A's seed must then not write mode v2 (nothing would manage the number again).


async def _handed_back_by_another_leader(rv, since=T0 + 50_000):
    """What leader B leaves behind: legacy, no longer v2-accounted, no busy list."""
    await rv.hset("bb:num:N1", mapping={"mode": "legacy", "mode_since_ms": since})
    await rv.srem("bb:v2:active", "N1")
    await rv.delete("bb:busy:N1")


async def test_a_stale_seed_cannot_undo_a_finished_hand_back(rv, db):
    await seed_number(rv, "N1", 5, {"T1": {}}, mode="v2_pending")
    await rv.hset("bb:num:N1", "mode_since_ms", T0)
    await rv.sadd("bb:v2:active", "N1")
    decided_on = await SWT._state(rv, "N1")  # leader A reads, decides SEED
    await _handed_back_by_another_leader(rv)
    db.live = [("P1", "OUTBOUND", "c1")]
    await SWT.apply_seed(rv, "N1", T0 + 60_000, set(), expected=decided_on)
    num = await rv.hgetall("bb:num:N1")
    assert num["mode"] == "legacy" and num["mode_since_ms"] == str(T0 + 50_000)
    assert not await rv.exists("bb:busy:N1")  # no holders added to a legacy number
    assert not await rv.sismember("bb:v2:active", "N1")
    assert len(await tickets_of(rv, "N1")) == 0  # and nothing issued


async def test_a_seed_whose_slow_db_read_was_overtaken_writes_nothing(
    rv, db, monkeypatch
):
    # No state passed: the step reads it itself, BEFORE its slow DB read.
    await seed_number(rv, "N1", 5, {"T1": {}}, mode="v2_pending")
    await rv.hset("bb:num:N1", "mode_since_ms", T0)

    async def slow_live_calls(number_id):
        await _handed_back_by_another_leader(rv)  # happens while the DB read is out
        return [("P1", "OUTBOUND", "c1")]

    monkeypatch.setattr(RC, "get_live_calls_on_number", slow_live_calls)
    await SWT.apply_seed(rv, "N1", T0 + 60_000)
    assert await rv.hget("bb:num:N1", "mode") == "legacy"
    assert not await rv.exists("bb:busy:N1")


async def test_a_stale_reseed_adds_no_holders_to_a_handed_back_number(rv, db):
    await seed_number(rv, "N1", 5, {"T1": {}})
    await rv.hset("bb:num:N1", mapping={"mode_since_ms": T0, "reseed_at_ms": T0})
    decided_on = await SWT._state(rv, "N1")
    await _handed_back_by_another_leader(rv)
    db.live = [("P9", "OUTBOUND", "c")]
    await SWT.apply_reseed(rv, "N1", set(), expected=decided_on)
    assert not await rv.exists("bb:busy:N1")


async def test_a_stale_drain_leaves_a_number_switched_on_again_alone(rv, db):
    await seed_number(rv, "N1", 5, {"T1": {}})
    await rv.hset("bb:num:N1", "mode_since_ms", T0)
    decided_on = await SWT._state(rv, "N1")  # v2, decided DRAIN
    await rv.hset("bb:num:N1", mapping={"mode": "v2_pending", "mode_since_ms": T0 + 9})
    await SWT.apply_drain(rv, "N1", T0 + 10, expected=decided_on)
    num = await rv.hgetall("bb:num:N1")
    assert num["mode"] == "v2_pending" and num["mode_since_ms"] == str(T0 + 9)


async def test_a_stale_hand_back_leaves_a_reseeded_number_alone(rv, db, monkeypatch):
    await seed_number(rv, "N1", 5, {"T1": {}}, mode="draining")
    await rv.hset("bb:num:N1", "mode_since_ms", T0)
    await rv.sadd("bb:v2:active", "N1")
    decided_on = await SWT._state(rv, "N1")  # draining, decided HANDOVER
    # meanwhile another leader handed it back, switched it on and seeded it again
    await rv.hset("bb:num:N1", mapping={"mode": "v2", "mode_since_ms": T0 + 99})
    await rv.sadd("bb:busy:N1", "lead:LIVE")
    await rv.zadd("bb:q:T1", {"W1": 1})
    moved = AsyncMock(return_value=1)
    monkeypatch.setattr(SWT.scripts, "move_room_to_schedule", moved)
    await SWT.apply_handover(rv, "N1", _number(), T0 + 100, expected=decided_on)
    num = await rv.hgetall("bb:num:N1")
    assert num["mode"] == "v2" and "handback_pending" not in num
    assert await rv.smembers("bb:busy:N1") == {"lead:LIVE"}
    assert await rv.sismember("bb:v2:active", "N1")
    moved.assert_not_awaited()  # its rooms stay its rooms
    db.channels.assert_not_awaited()  # today's counters untouched


async def test_a_hand_back_overtaken_half_way_is_finished_by_the_newer_one(
    rv, db, monkeypatch
):
    await seed_number(rv, "N1", 5, {"T1": {}}, mode="draining")
    await rv.hset("bb:num:N1", "mode_since_ms", T0)
    await rv.sadd("bb:v2:active", "N1")
    db.processing = {"N1": 0}

    async def count_while_a_newer_hand_back_flips_it():
        # a newer hand-back (another leader) re-ran step 2 while this one counted
        await rv.hset(
            "bb:num:N1",
            mapping={
                "mode": "legacy",
                "mode_since_ms": T0 + 500,
                "handback_pending": 1,
            },
        )
        return db.processing

    monkeypatch.setattr(
        SWT,
        "count_processing_by_telephony_number",
        count_while_a_newer_hand_back_flips_it,
    )
    await SWT.apply_handover(rv, "N1", _number(), T0 + 100)
    num = await rv.hgetall("bb:num:N1")
    # not finished by the overtaken step: the newer one owns the last step
    assert num["handback_pending"] == "1" and num["mode_since_ms"] == str(T0 + 500)
    assert await rv.sismember("bb:v2:active", "N1")


async def test_the_switch_passes_what_decide_saw_to_the_step(rv, db, monkeypatch):
    # decide() reads v2_pending; before the step runs the number moves on: no seed.
    _config(monkeypatch)
    await seed_number(rv, "N1", 5, {"T1": {}}, mode="v2_pending")
    await rv.hset(
        "bb:num:N1",
        mapping={"mode_since_ms": T0, "stable_count": 1, "stable_sig": "s"},
    )
    await rv.sadd("bb:v2:active", "N1")
    monkeypatch.setattr(SWT, "locked_signature", lambda leads: "s")
    real_apply_seed = SWT.apply_seed

    async def seed_after_a_hand_back(*a, **k):
        await _handed_back_by_another_leader(rv)
        return await real_apply_seed(*a, **k)

    monkeypatch.setattr(SWT, "apply_seed", seed_after_a_hand_back)
    await SWT.run_switch_step()
    assert await rv.hget("bb:num:N1", "mode") == "legacy"


async def test_switch_cas_refuses_an_op_outside_the_switch_vocabulary(rv):
    await rv.hset("bb:num:N1", mapping={"mode": "v2", "mode_since_ms": T0})
    res = await scripts.switch_cas("N1", "v2", T0, [["FLUSHDB"]])
    assert res is None  # an error reply, nothing run
    assert await rv.hget("bb:num:N1", "mode") == "v2"


async def test_a_hand_back_overtaken_while_moving_rooms_does_not_flip(
    rv, db, monkeypatch
):
    # The mode is still the one decided on when the step starts, and changes while its
    # rooms move (step 1): only the compare-and-set at the flip (step 2) can catch it.
    await seed_number(rv, "N1", 5, {"T1": {}}, mode="draining")
    await rv.hset("bb:num:N1", "mode_since_ms", T0)
    await rv.sadd("bb:v2:active", "N1")
    decided_on = await SWT._state(rv, "N1")

    async def move_while_another_leader_reseeds(template_id):
        await rv.hset("bb:num:N1", mapping={"mode": "v2", "mode_since_ms": T0 + 7})
        await rv.sadd("bb:busy:N1", "lead:LIVE")
        return 0

    monkeypatch.setattr(
        SWT.scripts, "move_room_to_schedule", move_while_another_leader_reseeds
    )
    await SWT.apply_handover(rv, "N1", _number(), T0 + 100, expected=decided_on)
    num = await rv.hgetall("bb:num:N1")
    assert num["mode"] == "v2" and "handback_pending" not in num
    assert await rv.smembers("bb:busy:N1") == {"lead:LIVE"}  # not wiped
    db.channels.assert_not_awaited()


# -- Swaroop's #1314 review ---------------------------------------------------------------


async def test_a_handback_from_v2_pending_leaves_todays_counters_alone(rv, db):
    """v2 issued nothing while pending and never moved today's counters, while today's
    dials it waited for may still be in flight: a recount now would undercount them."""
    await seed_number(rv, "N1", 5, {"T1": {}}, mode="v2_pending")
    state = await SWT._state(rv, "N1")
    await SWT.apply_handover(rv, "N1", _number(max_lines=5), T0, state)
    assert await rv.hget("bb:num:N1", "mode") == "legacy"
    db.channels.assert_not_awaited()


@pytest.mark.parametrize("began_in, recounts", [("v2_pending", False), ("", True)])
async def test_a_retried_handback_remembers_where_it_began(rv, db, began_in, recounts):
    """A hand-back cut short after its flip is re-run from legacy: one that began in
    v2_pending still must not recount today's counters (Swaroop, #1319)."""
    await seed_number(rv, "N1", 5, {"T1": {}}, mode="legacy")
    await rv.hset("bb:num:N1", mapping={"handback_pending": "1", "mode_since_ms": T0})
    if began_in:
        await rv.hset("bb:num:N1", "handback_from", began_in)
    state = await SWT._state(rv, "N1")
    await SWT.apply_handover(rv, "N1", _number(max_lines=5), T0 + 10, state)
    num = await rv.hgetall("bb:num:N1")
    assert "handback_pending" not in num and "handback_from" not in num
    assert db.channels.await_count == (1 if recounts else 0)


async def test_todays_worker_waits_while_a_handback_is_unfinished(rv, monkeypatch):
    from app.ai.voice.agents.breeze_buddy.dispatch import queue as queue_mod

    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=True))
    await rv.hset("bb:num:N1", mapping={"mode": "legacy", "handback_pending": "1"})
    await rv.set("bb:epoch", str(T0))
    assert (
        await queue_mod.v2_owns_number("N1") is None
    )  # wait: counters not written yet
    await rv.hdel("bb:num:N1", "handback_pending")
    assert await queue_mod.v2_owns_number("N1") is False


async def test_an_unresolved_route_fails_the_locked_leads_read(monkeypatch):
    """The template and config reads answer None on a DB error: the step must fail and
    retry, never drop that template's locked leads (they may be legacy dials in flight).
    """
    monkeypatch.setattr(
        RC, "get_legacy_inflight_leads", AsyncMock(return_value=[("K1", "T1", True)])
    )
    monkeypatch.setattr(R, "get_template_by_id", AsyncMock(return_value=None))
    with pytest.raises(RuntimeError):
        await RC.locked_leads_by_number()


def test_the_full_pass_runs_every_5_ticks_by_default():
    from app.core.config import static

    assert static.BB_V2_DUE_FULL_PASS_TICKS == 5

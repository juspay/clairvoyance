"""Switch numbers between today's dialler and v2 (design card §6b rules 21, 22, 27;
Fable C1, C3, I4, I8).

The only writer of ``bb:num:{N}.mode``:

    legacy (or absent) -> v2_pending -> v2 -> draining -> legacy

The dynamic config says which numbers *should* be on v2 (``is_desired``); the mode says
which *are*. Every 5 s the sweep leader runs ``run_switch_step``: for each number it
gathers the state and facts, ``decide`` (pure) picks at most one step, and one short
``apply_*`` coroutine performs it. Waiting is "not yet" on this check, never a sleep.

- **on**: ``v2_pending`` — leads go to rooms, today's workers bounce them, ``match`` issues
  nothing. After >= 15 s, once the number's locked leads (today's workers mid-dispatch)
  are the same on two checks in a row, the busy list is seeded from them and the live
  calls, and the mode becomes ``v2``; 20 s later it is seeded once more (adds only).
- **off**: ``draining`` — no new tickets; the ones out finish. When no ticket or lease is
  left, the number is handed back: mode ``legacy`` first, then DB ``channels`` = the DB's
  own PROCESSING count, today's tokens = ``max`` minus that, waiting leads to today's
  schedule. ``handback_pending`` marks a hand-back not finished yet; the next check retries
  it.

A failed read of the switch's config keys changes nothing on that check (a Redis blip must
not read as "v2 off").
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import (
    Any,
    Collection,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    cast,
)

from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    init_channel_semaphore,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import channel_key
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import keys as k, scripts
from app.ai.voice.agents.breeze_buddy.dispatch.v2.reconcile import (
    locked_leads_by_number,
    locked_signature,
    seed_holders,
    set_aside_parked,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import _client, refresh_number
from app.core.config import dynamic as dyn_cfg
from app.core.config.static import BB_V2_LOSS_RECOVERY_WAIT_S
from app.core.logger import logger
from app.database.accessor.breeze_buddy.dispatch import (
    count_processing_by_telephony_number,
    get_telephony_numbers_by_ids,
    list_telephony_numbers,
    set_telephony_number_channels,
)
from app.schemas import CallProvider, TelephonyNumber, TelephonyNumberStatus

LEGACY, V2_PENDING, V2, DRAINING = "legacy", "v2_pending", "v2", "draining"

# >= 3 refreshes of every pod's v2_seen latch (5 s) and over Plivo's 15 s request timeout
PENDING_MIN_MS = 15_000
STABLE_CHECKS = 2
RESEED_AFTER_MS = 20_000
# After a Redis loss that leaves no number to put back on v2 (the flags went too), today's
# counters are rebuilt from the DB this long after the loss was seen: by then a dial in
# flight at the loss has returned: today's greeting pre-warm (up to 60.5 s) plus the
# slowest provider's request timeout (Vobiz, 30 s); static.py.
LEGACY_RECOVERY_WAIT_MS = BB_V2_LOSS_RECOVERY_WAIT_S * 1000
# DB ``channels`` has two writers on the sweep leader: the v2 mirror and the hand-back.
# Each holds this across its mode check and write, so an older count never lands last.
CHANNELS_LOCK = asyncio.Lock()
# Plivo and Vobiz (card rule 27). One lead can briefly hold tickets on two numbers (its
# template moved while a ticket was out, and the backlog job re-queued it on the new
# number). Plivo and Vobiz share today's ``channels`` gate and the same dial path: both
# write the lead's PROCESSING row after the provider's reply (#1280's dial-row-first is not
# in this release), so v2 treats them alike. Twilio's gate is the number's status, not a
# line count. Exotel
# shares today's ``channels`` gate but stays too: its inbound calls are not gated, so a busy
# list could not count them, and a call it places may come back without a call id, which v2
# would hold a line for until the 10-min stuck-dial reap.
V2_PROVIDERS = frozenset({CallProvider.PLIVO, CallProvider.VOBIZ})

# Steps
NONE, TURN_ON, OBSERVE, SEED, RESEED, DRAIN, HANDOVER = (
    "none",
    "turn_on",
    "observe",
    "seed",
    "reseed",
    "drain",
    "handover",
)


@dataclass(frozen=True)
class NumState:
    """What ``bb:num:{N}`` says about the number's switch."""

    mode: str = LEGACY
    mode_since_ms: int = 0
    stable_count: int = 0
    stable_sig: str = ""
    reseed_at_ms: int = 0  # 0 = no re-seed due
    handback_pending: bool = False  # legacy, but the hand-back's DB/room steps not done
    handback_from: str = ""  # v2_pending when that hand-back began there

    @classmethod
    def from_hash(cls, h: Mapping[str, str]) -> "NumState":
        def num(field: str) -> int:
            return int(h.get(field) or 0)

        return cls(
            mode=h.get("mode") or LEGACY,
            mode_since_ms=num("mode_since_ms"),
            stable_count=num("stable_count"),
            stable_sig=h.get("stable_sig", ""),
            reseed_at_ms=num("reseed_at_ms"),
            handback_pending=h.get("handback_pending") == "1",
            handback_from=h.get("handback_from", ""),
        )


@dataclass(frozen=True)
class Facts:
    now_ms: int
    drained: bool = False  # no lease left on the number (every ticket has one)
    sig: Optional[str] = None  # the number's locked leads; None = unreadable


@dataclass(frozen=True)
class Step:
    action: str
    stable_count: int = 0
    stable_sig: str = ""


def is_desired(
    number: Optional[TelephonyNumber], enabled: bool, configured: Collection[str]
) -> bool:
    """Should v2 serve this number? The flag, the list, and a number that today's path
    could dial right now on a provider v2 supports (rule 27: a non-AVAILABLE number drains
    to today's path, where its leads end NUMBER_UNAVAILABLE as before)."""
    return (
        enabled
        and number is not None
        and number.id in configured
        and number.status == TelephonyNumberStatus.AVAILABLE
        and number.provider in V2_PROVIDERS
    )


def decide(state: NumState, desired: bool, facts: Facts) -> Step:
    """The next step for one number. Pure."""
    # v2 off for a number: hand it back now, no draining (a hard stop, by rule)
    if state.mode in (V2_PENDING, V2, DRAINING) and not desired:
        return Step(HANDOVER)
    if state.mode == V2_PENDING:
        return _pending_step(state, facts)
    if state.mode == V2:
        if state.reseed_at_ms and facts.now_ms >= state.reseed_at_ms:
            return Step(RESEED)
        return Step(NONE)
    if state.mode == DRAINING:  # a leftover of an older drain: finish it
        return Step(HANDOVER)
    if state.handback_pending:
        return Step(HANDOVER)  # a hand-back failed half-way: finish it before anything
    return Step(TURN_ON if desired else NONE)  # legacy or absent


def _pending_step(state: NumState, facts: Facts) -> Step:
    if facts.sig is None:
        return Step(OBSERVE)  # can't tell: start counting again
    count = state.stable_count + 1 if facts.sig == state.stable_sig else 1
    if count >= STABLE_CHECKS and facts.now_ms - state.mode_since_ms >= PENDING_MIN_MS:
        return Step(SEED)
    return Step(OBSERVE, stable_count=count, stable_sig=facts.sig)


# ---------------------------------------------------------------------------
# Apply steps: each one short Redis/DB step; any error leaves the mode as it was
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


# Every apply step writes through ``scripts.switch_cas``: only if the number's mode and
# mode_since_ms are still the ones the step was decided on (``expected``; read here when
# not given, before any slow part of the step). A step of a deposed sweep leader, or one
# overtaken by a newer step, changes nothing (review #1287 finding 3).


async def _state(c: Any, number_id: str) -> NumState:
    return NumState.from_hash(await c.hgetall(k.num_key(number_id)))


def _same_mode(a: NumState, b: NumState) -> bool:
    return a.mode == b.mode and a.mode_since_ms == b.mode_since_ms


async def _cas(
    number_id: str, expected: NumState, action: str, ops: List[List[str]]
) -> bool:
    """``ops`` applied, all or none, if the number is still ``expected``'s mode. False:
    stale, nothing written. A Redis error raises (the next check retries the step)."""
    applied = await scripts.switch_cas(
        number_id, expected.mode, expected.mode_since_ms, ops
    )
    if applied is None:
        raise RuntimeError(f"v2 switch {action}: compare-and-set failed")
    if not applied:
        logger.warning(
            f"v2 switch: {action} for {number_id} skipped, its mode changed since it "
            f"was read ({expected.mode} since {expected.mode_since_ms})"
        )
    return applied


async def apply_turn_on(
    c: Any,
    number: TelephonyNumber,
    now_ms: int,
    expected: Optional[NumState] = None,
) -> None:
    """legacy -> v2_pending: from now the number's leads go to rooms and today's workers
    bounce them back there; ``match`` issues nothing yet."""
    if expected is None:
        expected = await _state(c, number.id)
    await refresh_number(number)  # max / provider / status for match
    num = k.num_key(number.id)
    ops = [
        ["SADD", k.V2_ACTIVE_KEY, number.id],
        ["HSET", num, "mode", V2_PENDING, "mode_since_ms", str(now_ms)],
        ["HSET", num, "stable_count", "0", "stable_sig", ""],
        ["HDEL", num, "reseed_at_ms"],
    ]
    if await _cas(number.id, expected, TURN_ON, ops):
        logger.info(f"v2 switch: {number.id} -> v2_pending")


async def apply_observe(
    c: Any, number_id: str, step: Step, expected: Optional[NumState] = None
) -> None:
    if expected is None:
        expected = await _state(c, number_id)
    num = k.num_key(number_id)
    ops = [
        ["HSET", num, "stable_count", str(step.stable_count)],
        ["HSET", num, "stable_sig", step.stable_sig],
    ]
    await _cas(number_id, expected, OBSERVE, ops)


async def _seed_ops(number_id: str, locked: Optional[Set[str]]) -> List[List[str]]:
    holders = await seed_holders(number_id, locked)
    return [["SADD", k.busy_key(number_id), *sorted(holders)]] if holders else []


async def apply_seed(
    c: Any,
    number_id: str,
    now_ms: int,
    locked: Optional[Set[str]] = None,
    expected: Optional[NumState] = None,
) -> None:
    """v2_pending -> v2: the busy list now holds every live call and every lead today's
    dialler is still dispatching, so ``match`` issues only the lines really free.
    ``locked``: the number's locked leads, if already read in this step (read first).
    The holders and the mode are written together, and only if the number is still
    pending: the DB read in between can be slow (review #1287 finding 3)."""
    if expected is None:
        expected = await _state(c, number_id)
    num = k.num_key(number_id)
    ops = await _seed_ops(number_id, locked)
    ops += [
        ["HSET", num, "mode", V2, "mode_since_ms", str(now_ms)],
        ["HSET", num, "reseed_at_ms", str(now_ms + RESEED_AFTER_MS)],
        ["HDEL", num, "stable_count", "stable_sig"],
    ]
    if not await _cas(number_id, expected, SEED, ops):
        return
    logger.info(f"v2 switch: {number_id} -> v2")
    await scripts.match(number_id)


async def apply_reseed(
    c: Any,
    number_id: str,
    locked: Optional[Set[str]] = None,
    expected: Optional[NumState] = None,
) -> None:
    """Once, 20 s into v2: legacy dials that turned PROCESSING after the seed (adds only)."""
    if expected is None:
        expected = await _state(c, number_id)
    ops = await _seed_ops(number_id, locked)
    ops.append(["HDEL", k.num_key(number_id), "reseed_at_ms"])
    await _cas(number_id, expected, RESEED, ops)


async def apply_drain(
    c: Any, number_id: str, now_ms: int, expected: Optional[NumState] = None
) -> None:
    """-> draining: ``match`` stops issuing; tickets already out are dialled or given back."""
    if expected is None:
        expected = await _state(c, number_id)
    for template_id in sorted(await c.smembers(k.numtpl_key(number_id))):
        # a draining number gives out no lines: calls with no lead row (bb:qi) would
        # never leave, and the hand-back would wait for them for ever
        if await c.hlen(k.qi_key(template_id)):
            logger.error(
                f"v2 switch: {number_id} stays v2, calls with no lead row wait in the "
                f"room of {template_id}: remove it from BB_V2_INTENT_NUMBERS and wait "
                "for its waiting calls to be placed or withdrawn"
            )
            return
    num = k.num_key(number_id)
    ops = [
        ["HSET", num, "mode", DRAINING, "mode_since_ms", str(now_ms)],
        ["HDEL", num, "stable_count", "stable_sig", "reseed_at_ms"],
    ]
    if await _cas(number_id, expected, DRAIN, ops):
        logger.info(f"v2 switch: {number_id} -> draining")


def _max_lines(number: Optional[TelephonyNumber]) -> int:
    """Like today's reconciler: a NULL ``maximum_channels`` counts as 1."""
    if number is None:
        return 0
    return 1 if number.maximum_channels is None else int(number.maximum_channels)


async def _reset_channel_tokens(c: Any, number_id: str, free: int) -> None:
    """Today's token list with ``free`` tokens, built by today's helper."""
    if free > 0:
        if not await init_channel_semaphore(number_id, free):
            raise RuntimeError(f"channel tokens not reset for {number_id}")
    else:
        await c.delete(channel_key(number_id))  # every line taken: no tokens


async def _reset_todays_counters(
    c: Any, number_id: str, number: Optional[TelephonyNumber]
) -> Tuple[int, Optional[int]]:
    """Today's counters for the number, from the DB: ``channels`` = the DB's own PROCESSING
    count, the one today's reconciler uses (never SCARD of the busy list, which may hold
    stale holders: Fable C3), and today's token list = ``max`` minus that count. Today's
    reconciler keeps no tokens for a DISABLED number, and neither does this. Returns the
    count and the tokens set (None: DISABLED)."""
    in_flight = (await count_processing_by_telephony_number()).get(number_id, 0)
    await set_telephony_number_channels(number_id, in_flight)
    if number is not None and number.status == TelephonyNumberStatus.DISABLED:
        return in_flight, None
    free = _max_lines(number) - in_flight
    await _reset_channel_tokens(c, number_id, free)
    return in_flight, max(0, free)


async def _move_rooms(templates: Iterable[str]) -> None:
    for template_id in sorted(templates):
        if await scripts.move_room_to_schedule(template_id) is None:
            raise RuntimeError(f"room of {template_id} not moved")


async def apply_handover(
    c: Any,
    number_id: str,
    number: Optional[TelephonyNumber],
    now_ms: int,
    expected: Optional[NumState] = None,
) -> None:
    """draining (or v2_pending) -> legacy, once no ticket or lease is left. Every step is
    idempotent: a failure leaves ``handback_pending`` set and the next check re-runs all of
    it. A room of the number's templates is always listed in ``bb:numtpl:{N}`` (enqueue
    and the reaper add it); one whose SADD was lost is moved by the orphan prune.
    The flip to legacy happens only if the number is still ``expected``'s mode, and the
    hand-back is finished only by the step that flipped it (review #1287 finding 3)."""
    if expected is None:
        expected = await _state(c, number_id)
    elif not _same_mode(await _state(c, number_id), expected):
        logger.warning(f"v2 switch: handover for {number_id} skipped, mode changed")
        return
    templates = sorted(await c.smembers(k.numtpl_key(number_id)))
    for template_id in templates:
        await set_aside_parked(c, template_id)
    # 1. rooms to today's schedule while v2 still owns the number
    await _move_rooms(templates)
    # 2. legacy first: from here calls release, inbound admits and enqueues use today's
    #    path, so the count below includes no call that will later release through v2
    num = k.num_key(number_id)
    flip = [
        ["HSET", num, "mode", LEGACY, "mode_since_ms", str(now_ms)],
        ["HSET", num, "handback_pending", "1"],
        ["HDEL", num, "stable_count", "stable_sig", "reseed_at_ms"],
        ["ZREM", k.DUE_KEY, number_id],
        # its entries left in bb:tickets fail claim (no lease): an acceptor drops them
        ["UNLINK", k.busy_key(number_id), k.inflight_key(number_id)],
    ]
    if expected.mode == V2_PENDING:
        flip.append(["HSET", num, "handback_from", V2_PENDING])  # a retry must know
    async with CHANNELS_LOCK:
        if not await _cas(number_id, expected, HANDOVER, flip):
            return
        if V2_PENDING in (expected.mode, expected.handback_from):
            # v2 issued nothing and never moved today's counters, while today's dials it
            # waited for may still be in flight: recounting now would undercount them
            in_flight, tokens = None, None
        else:
            # 3-4. DB channels = the DB's own PROCESSING count, today's tokens = max - that
            in_flight, tokens = await _reset_todays_counters(c, number_id, number)
    # 5. rooms again, listed now: a lead enqueued during step 1 (the mode was still
    #    draining), even of a template first routed to the number meanwhile
    await _move_rooms(await c.smembers(k.numtpl_key(number_id)))
    # 6. done, if the hand-back is still this step's (a newer re-run finishes its own)
    ours = NumState(mode=LEGACY, mode_since_ms=now_ms, handback_pending=True)
    done = [
        ["HDEL", num, "handback_pending", "handback_from"],
        ["SREM", k.V2_ACTIVE_KEY, number_id],
    ]
    if not await _cas(number_id, ours, HANDOVER, done):
        return
    logger.info(
        f"v2 switch: {number_id} -> legacy (channels={in_flight}, "
        f"tokens={'-' if tokens is None else tokens})"
    )


# ---------------------------------------------------------------------------
# The switch job (sweep leader, every 5 s) and the flush recovery
# ---------------------------------------------------------------------------


class _LockedLeads:
    """``locked_leads_by_number``, read at most once per switch step, on first use, and
    shared by every number's signature and seed (Fable I2). A failed read fails every
    use in the step; the next step reads again."""

    def __init__(self) -> None:
        self._by_number: Optional[Dict[str, Set[str]]] = None
        self._error: Optional[Exception] = None

    async def of(self, number_id: str) -> Set[str]:
        if self._by_number is None and self._error is None:
            try:
                self._by_number = await locked_leads_by_number()
            except Exception as e:  # noqa: BLE001 — re-raised to every caller below
                logger.warning(f"v2 switch: locked leads unreadable: {e}")
                self._error = e
        if self._by_number is None:
            raise RuntimeError(f"locked leads unreadable: {self._error}")
        return set(self._by_number.get(number_id, ()))


async def _facts(
    c: Any,
    number_id: str,
    state: NumState,
    desired: bool,
    now_ms: int,
    locked: _LockedLeads,
) -> Facts:
    drained = False
    if state.mode in (V2_PENDING, DRAINING):
        # every ticket out has a lease (issued, claimed or dialling): none = drained
        drained = await c.hlen(k.inflight_key(number_id)) == 0
    sig = None
    if state.mode == V2_PENDING and desired:
        try:
            sig = locked_signature(await locked.of(number_id))
        except RuntimeError:
            pass  # can't tell: the count starts again
    return Facts(now_ms=now_ms, drained=drained, sig=sig)


async def _apply(
    c: Any,
    number_id: str,
    step: Step,
    number: Optional[TelephonyNumber],
    now_ms: int,
    locked: _LockedLeads,
    state: NumState,
) -> None:
    """``state``: what ``decide`` saw; every write is conditional on it still holding."""
    if step.action == TURN_ON:
        await apply_turn_on(c, cast(TelephonyNumber, number), now_ms, state)
    elif step.action == OBSERVE:
        await apply_observe(c, number_id, step, state)
    elif step.action == SEED:
        await apply_seed(c, number_id, now_ms, await locked.of(number_id), state)
    elif step.action == RESEED:
        await apply_reseed(c, number_id, await locked.of(number_id), state)
    elif step.action == DRAIN:
        await apply_drain(c, number_id, now_ms, state)
    elif step.action == HANDOVER:
        await apply_handover(c, number_id, number, now_ms, state)


async def _read_config() -> Tuple[bool, List[str]]:
    """The flag and the number list; raises if either can't be read (ruling C-concern 4:
    the default "off" / "none" would drain every number)."""
    enabled = await dyn_cfg.BB_DISPATCH_V2_ENABLED(strict=True)
    configured = await dyn_cfg.BB_DISPATCH_V2_NUMBERS(strict=True)
    return enabled, configured


_warned_unsupported: Set[str] = set()


def _warn_unsupported(
    configured: Collection[str], numbers: Dict[str, TelephonyNumber]
) -> None:
    """A listed number on a provider v2 does not dial (rule 27) stays on today's path:
    say so once per process, not every check."""
    for number_id in configured:
        number = numbers.get(number_id)
        if (
            number is not None
            and number.provider not in V2_PROVIDERS
            and number_id not in _warned_unsupported
        ):
            _warned_unsupported.add(number_id)
            logger.warning(
                f"v2 switch: {number_id} is listed in BB_DISPATCH_V2_NUMBERS but is a "
                f"{number.provider.value} number; v2 dials Plivo and Vobiz only, so it stays on "
                "today's path"
            )


async def run_switch_step() -> None:
    """One check of every configured or v2-accounted number. A config or DB read error
    skips the whole check (raises); an error on one number skips only that number."""
    c = await _client()
    enabled, configured = await _read_config()
    number_ids = sorted(set(configured) | set(await c.smembers(k.V2_ACTIVE_KEY)))
    if not number_ids:
        return
    numbers = await get_telephony_numbers_by_ids(number_ids)
    _warn_unsupported(configured, numbers)
    now_ms = _now_ms()
    locked = _LockedLeads()
    plans: List[Tuple[str, Step, NumState]] = []
    for number_id in number_ids:
        state = await _state(c, number_id)
        desired = is_desired(numbers.get(number_id), enabled, configured)
        facts = await _facts(c, number_id, state, desired, now_ms, locked)
        plans.append((number_id, decide(state, desired, facts), state))
    # Stop new tickets everywhere first: a global off drains every number in this pass.
    plans.sort(key=lambda plan: plan[1].action != DRAIN)
    for number_id, step, state in plans:
        if step.action == NONE:
            continue
        try:
            await _apply(
                c, number_id, step, numbers.get(number_id), now_ms, locked, state
            )
        except Exception as e:  # noqa: BLE001 — retried on the next check
            logger.error(f"v2 switch {step.action} failed for {number_id}: {e}")


async def recover_after_flush(lost_for_ms: int = 0, *, first_use: bool = False) -> bool:
    """``bb:epoch`` is missing: Redis lost v2's keys (a restart, failover or flush), or v2
    runs for the first time (``first_use``: the sweeper's process never saw the epoch
    set). After a loss, until the epoch is set again, today's workers dial on no number
    (``queue.v2_owns_number``). Returns True once it is set; a new leader repeats an
    unfinished recovery (every step is idempotent).

    - Some number should be on v2 (the flags survived, e.g. also in the environment):
      every desired number restarts at ``v2_pending`` — its leads go to rooms and today's
      workers stop dialling it at once — and the normal switch-on seeds it; the >= 15 s
      pending covers Plivo's request timeout (rule 9). The epoch is set at once; the
      backlog reconciler refills the rooms.
    - None should, on first use: v2 never ran, so today's counters are its own; the
      epoch is just set, at once.
    - None should, after a loss (the flags went with Redis: x7b): every number is on
      today's path, and v2 never kept today's DB ``channels`` right. Once ``lost_for_ms``
      (since the loss was first seen) reaches ``LEGACY_RECOVERY_WAIT_MS``, every
      Plivo/Vobiz number gets the hand-back's counters from the DB, then the epoch,
      marked as a legacy recovery.
    """
    c = await _client()
    enabled, configured = await _read_config()
    numbers: Dict[str, TelephonyNumber] = {}
    if enabled and configured:
        numbers = await get_telephony_numbers_by_ids(configured)
    desired = [n for n in configured if is_desired(numbers.get(n), enabled, configured)]
    now_ms = _now_ms()
    if desired or first_use:
        for number_id in desired:
            await apply_turn_on(c, numbers[number_id], now_ms)
        await c.set(k.EPOCH_KEY, str(now_ms))
        logger.info("v2: epoch set after a Redis flush (or first use)")
        return True
    if lost_for_ms < LEGACY_RECOVERY_WAIT_MS:
        return False  # dials in flight at the loss are still settling; the hold goes on
    for number in await list_telephony_numbers():
        if number.provider in V2_PROVIDERS:
            await _reset_todays_counters(c, number.id, number)
    await c.set(k.EPOCH_KEY, f"legacy-recovery:{_now_ms()}")
    logger.info("v2: Redis lost the v2 flags; today's counters rebuilt, epoch set")
    return True

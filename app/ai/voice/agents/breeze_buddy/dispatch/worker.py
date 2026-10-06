"""
Dispatch worker (Plane 4) — pops a lead, sets up the call, hands off.

Each pod runs ``BB_WORKER_COUNT`` long-lived workers. The worker is the
"phone-dispatcher" in the call-centre analogy: it doesn't talk on the call,
it sets up the dial and lets the voice agent take over once the customer
answers.

Flow (matches docs/BACKLOG_DISPATCHER_REDESIGN.md §2 Plane 4):

    BLPOP ready -> RPUSH processing -> DB CAS lock -> pre-checks
      -> calling-hours -> rate-limit -> pick number -> BLPOP channel
      -> provider.make_call -> UPDATE status=PROCESSING -> LREM processing

Crash recovery is handled by ``reap_stuck_processing_lists``: the
``bb:processing:leads:{worker_uuid}`` list and the heartbeat key make every
in-flight pick visible to the reaper.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar, cast

import aiohttp

from app.ai.voice.agents.breeze_buddy.accounts import AccountRefused, Accounts
from app.ai.voice.agents.breeze_buddy.crm_mirror import (
    is_non_customer_lead,
    mirror_to_crm,
)
from app.ai.voice.agents.breeze_buddy.dispatch.alerts import (
    raise_call_limit_unavailable,
    raise_no_telephony_number,
)
from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    acquire_channel_token,
    capacity_defer_seconds,
    release_channel_token,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    READY_LIST,
    processing_list_for,
    reseller_paused_key,
    worker_heartbeat_key,
)
from app.ai.voice.agents.breeze_buddy.dispatch.queue import (
    is_dispatchable,
    requeue_in_room,
    schedule_lead,
    v2_owns_number,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    scripts as v2_scripts,
    throttle as v2_throttle,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2.memo import TTLMemo
from app.ai.voice.agents.breeze_buddy.managers.calls import (
    _acquire_number,
    _get_available_number,
    _get_lead_config,
    _is_within_calling_hours,
    _release_number,
    _run_pre_checks_for_lead,
    finish_lead_call_limit_reached,
)
from app.ai.voice.agents.breeze_buddy.managers.pre_checks import PreCheckDecision
from app.ai.voice.agents.breeze_buddy.managers.utils import (
    prepare_and_store_initial_greeting,
)
from app.ai.voice.agents.breeze_buddy.services.call_limiter import (
    CALL_LIMIT_UNAVAILABLE_REASON,
    CallLimitUnavailable,
    merchant_call_limits,
    peek_call_limit,
    peek_outbound_rate_limit_and_alert,
    record_call_limit,
    record_outbound_call_attempt,
    unrecord_call_limit,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    DIAL_OUTCOME_UNKNOWN,
    UNKNOWN_DIAL_META_KEY,
    dial_ref_time,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.utils import get_voice_provider
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.utils.common import _gemini_realtime_config
from app.ai.voice.agents.breeze_buddy.utils.playground import (
    apply_playground_overrides,
)
from app.core.concurrency import spawn_background_task
from app.core.config import dynamic as dyn_cfg
from app.core.config.static import (
    BB_V2_DIAL_MEMO_TTL_S,
    BB_V2_PREWARM_WAIT_S,
    BB_V2_TTS_CONCURRENCY,
    BB_WORKER_BLPOP_TIMEOUT_S,
    BB_WORKER_COUNT,
    BB_WORKER_HEARTBEAT_REFRESH_S,
    BB_WORKER_HEARTBEAT_TTL_S,
    BB_WORKER_SHUTDOWN_DRAIN_S,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor import (
    acquire_lock_on_lead_by_id,
    attach_placed_call_to_lead,
    defer_lead_next_attempt_and_release_lock,
    get_lead_by_id,
    get_template_by_id,
    hold_unknown_dial,
    is_number_blacklisted,
    release_lock_on_lead_by_id,
    revert_dial_to_backlog,
    stamp_dialled_call,
    update_lead_call_completion_details,
    update_lead_call_details,
)
from app.schemas import CallProvider, ExecutionMode, LeadCallStatus
from app.services.redis import get_redis_service

# A held-line lead whose lock a stale ticket's task still holds is retried this far out.
V2_LOCK_RETRY_S = 30
# A lead picked this much before its row's next_attempt_at is re-scheduled, not dialled
# (stale queue copies); small jitter in schedules stays well inside it.
EARLY_PICK_TOLERANCE_S = 5
# A lead on a v2 number that can't go back to its room (mode unreadable, no route) is
# deferred this far, so it is never bounced in a loop.
V2_REDIRECT_RETRY_S = 30


@dataclass
class ClaimedTicket:
    """The v2 ticket an acceptor coroutine claimed for a lead, i.e. a line it holds: its
    number, its ticket id, the owner id the ticket was claimed under (mark_dialling and
    the give-back act only on a lease that owner holds), and its acceptor's stop signal
    (a dial Plivo keeps refusing gives up at once when the pod stops)."""

    number_id: str
    tk: int  # the ticket id, as on ``scripts.Ticket``
    owner: str
    stopping: asyncio.Event


class _DispatchLine:
    """The line a dispatch holds, so every give-back is one call: today's channel
    token + DB channel, or a v2 ticket's lease (``ticket``)."""

    def __init__(
        self,
        number: Any = None,
        token: Any = None,
        ticket: Optional[ClaimedTicket] = None,
        lead_id: Optional[str] = None,
    ):
        self._number = number
        self._token = token
        self._ticket = ticket
        self._lead_id = lead_id
        self._given = False
        # v2: the provider call is (about to be) placed; the line now belongs to it.
        self.dialling = False
        # v2: the give-back found the lease no longer this ticket's (reaped or
        # re-issued): the lead's lock and schedule belong to its new holder.
        self.not_ours = False

    async def give_back(self, not_placed: bool = False) -> None:
        """``not_placed``: the provider said no call was placed (v2 only: after the
        dial was marked, any other give-back is refused and the line stays held)."""
        if self._given:
            return
        if self._ticket is not None:
            if self.dialling and not not_placed:
                return
            # Conditional on our ticket id: a no-op if the lease was reaped or re-issued.
            # None = Redis failed: retry once, and only then count it as given back
            # (a later plain give-back is refused once the dial is marked).
            for _ in range(2):
                result = await v2_scripts.return_line(
                    self._ticket.number_id,
                    str(self._lead_id),
                    self._ticket.tk,
                    self._ticket.owner,
                    not_placed=not_placed,
                )
                if result is not None:
                    self._given = True
                    self.not_ours = result == v2_scripts.GiveBack.NOT_OURS
                    return
            return
        self._given = True
        await release_channel_token(self._number.id, self._token)
        await _release_number(self._number.id, self._number.provider)


async def _invalidate_route(template_id: Optional[str]) -> None:
    if not template_id:
        return
    # lazy: routes -> managers.calls -> dispatch (import cycle)
    from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import invalidate_route

    await invalidate_route(template_id)


# ---------------------------------------------------------------------------
# Greeting pre-warm
# ---------------------------------------------------------------------------

# One retry on ANY failure — timeouts included. A first attempt that runs
# out of time is usually cold-start (Live session connect + model warm-up),
# so the retry frequently lands. Per-attempt caps: 15s for TTS greetings,
# 30s for the Gemini Live opening line (measured: a 41-word greeting
# generates in ~15s over Live — the TTS cap would fail most non-trivial
# Live greetings). Worst-case channel-token hold is timeout + 0.5s pause
# + timeout (~30.5s TTS / ~60.5s Live), only reached when generation is
# fully degraded — which is when dials should pace down anyway.
_GREETING_PREWARM_ATTEMPTS = 2
_GREETING_PREWARM_RETRY_PAUSE_S = 0.5
_GREETING_PREWARM_TIMEOUT_S = 15.0
_GREETING_PREWARM_LIVE_TIMEOUT_S = 30.0


async def _prewarm_initial_greeting_with_retry(
    lead_id: str,
    payload: Dict[str, Any],
    template: TemplateModel,
) -> None:
    """
    Pre-dial greeting preparation, AWAITED so audio is in Redis before the
    phone rings: TTS synthesis for non-realtime templates and — via
    generate_realtime_opening_line — the Gemini Live opening line
    (per-TEMPLATE, normally generated at template-save time, so a cheap
    Redis GET on the happy path).

    Runs after every dispatch gate (rate-limit record, channel token, DB
    number), immediately before the dial — TTS/generation spend is
    proportional to actual dials. Each attempt is capped at
    _GREETING_PREWARM_TIMEOUT_S (TTS) or _GREETING_PREWARM_LIVE_TIMEOUT_S
    (Gemini Live opening line); the single retry fires on ANY failure —
    timeout included — so the worst-case channel-token hold is
    timeout + pause + timeout. Every failure path is fail-open: we dial
    anyway and the answer-time path in agent setup retries synthesis as a
    cache miss (worst case the caller hears the dial-tone fallback / LLM
    speaks first).
    """
    greeting_expected = bool(
        template.configurations and template.configurations.initial_greeting
    )
    timeout_s = (
        _GREETING_PREWARM_LIVE_TIMEOUT_S
        if _gemini_realtime_config(template) is not None
        else _GREETING_PREWARM_TIMEOUT_S
    )
    for attempt in range(1, _GREETING_PREWARM_ATTEMPTS + 1):
        try:
            result = await asyncio.wait_for(
                prepare_and_store_initial_greeting(
                    lead_id=lead_id,
                    payload=payload,
                    template=template,
                    generate_realtime_opening_line=True,
                ),
                timeout=timeout_s,
            )
            if result is not None or not greeting_expected:
                return  # cached (or nothing configured) — done
            logger.warning(
                f"Greeting prewarm attempt {attempt}/{_GREETING_PREWARM_ATTEMPTS} "
                f"fail-opened with no cached audio for lead {lead_id}; "
                "answer-time path will retry"
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Greeting prewarm attempt {attempt}/{_GREETING_PREWARM_ATTEMPTS} "
                f"timed out for lead {lead_id}; retrying — first attempts "
                "often time out on cold-start"
            )
        except Exception as e:  # noqa: BLE001
            logger.opt(exception=e).warning(
                f"Greeting prewarm attempt {attempt}/{_GREETING_PREWARM_ATTEMPTS} "
                f"failed for lead {lead_id}; answer-time path will retry"
            )
        if attempt < _GREETING_PREWARM_ATTEMPTS:
            await asyncio.sleep(_GREETING_PREWARM_RETRY_PAUSE_S)


# v2 (spec 2026-10-05 §4.7, decision D10): greeting syntheses at once per pod. Without
# the dial-task pool a burst could start one per ticket; past the limit a prewarm waits
# its timeout, then is skipped (fail-open: the answer path synthesises on a cache miss).
_TTS_SLOTS = asyncio.Semaphore(BB_V2_TTS_CONCURRENCY)

# v2 (spec 2026-10-05 §4.10): a held dial's template, call config and number, kept
# BB_V2_DIAL_MEMO_TTL_S per pod. Today's path reads them per dial, as before.
_DIAL_MEMO = TTLMemo(ttl_s=BB_V2_DIAL_MEMO_TTL_S)

# v2 (design card rule 51): the 429 loop of a held dial.
_THROTTLE = v2_throttle.Throttle()


_R = TypeVar("_R")


async def _dial_read(
    memo_tid: Optional[str], key: Tuple[Any, ...], load: Callable[[], Awaitable[_R]]
) -> _R:
    """One of ``_dispatch_lead``'s per-template reads: through the pod's memo under
    ``key`` on a held v2 line (``memo_tid`` set), else read now (today's path)."""
    if memo_tid:
        return await _DIAL_MEMO.get(key, load)
    return await load()


async def _template_by_id(template_id: Optional[str]) -> Any:
    return await get_template_by_id(template_id) if template_id else None


def _forget_dial_memo(lead: Any) -> None:
    """Drop a template's memo entries (the keys ``_dispatch_lead`` reads them under)."""
    _DIAL_MEMO.forget(("cfg", lead.template_id, lead.template))
    _DIAL_MEMO.forget(("tpl", lead.template_id))
    _DIAL_MEMO.forget(("num", lead.template_id))


async def _prewarm_in_slot(
    lead_id: str, payload: Dict[str, Any], template: Any
) -> None:
    try:
        await asyncio.wait_for(
            _TTS_SLOTS.acquire(), timeout=_GREETING_PREWARM_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        logger.warning(f"Greeting prewarm skipped for lead {lead_id}: TTS slots busy")
        return
    try:
        await _prewarm_initial_greeting_with_retry(
            lead_id=lead_id, payload=payload, template=template
        )
    finally:
        _TTS_SLOTS.release()


async def _prewarm_beside_the_dial(
    lead_id: str, payload: Dict[str, Any], template: Any
) -> None:
    """v2 (spec §4.7, decision D2): the prewarm starts where today's does (after every
    gate, so TTS spend stays proportional to dials), but the dial waits for it at most
    BB_V2_PREWARM_WAIT_S; it goes on through the dial and the ring."""
    task = spawn_background_task(
        _prewarm_in_slot(lead_id, payload, template), name=f"bb-prewarm-{lead_id}"
    )
    await asyncio.wait({task}, timeout=BB_V2_PREWARM_WAIT_S)


# How long a lead waits when the merchant's call rule can't be evaluated
# (Redis down, rule unreadable). Short: it is a transient, not a verdict.
CALL_LIMIT_UNAVAILABLE_DEFER_S = 30


# ---------------------------------------------------------------------------
# Single dispatch worker
# ---------------------------------------------------------------------------


# Worker._phase values — see Worker.stop().
_IDLE = "idle"
_PRE_DIAL = "pre_dial"  # checks before the channel is taken: nothing held
_HOLDING = "holding"  # channel token / DB channel taken (short steps, never cancelled)
_PREWARM = "prewarm"  # greeting pre-warm: gives its channel back if cancelled
_COMMITTED = "committed"
# stop() cancels a dispatch in these phases (it holds nothing a cancel would leak)
# and waits for the others.
_CANCELLABLE = frozenset({_IDLE, _PRE_DIAL, _PREWARM})


class Worker:
    """
    Long-lived asyncio task that consumes from ``bb:ready:leads`` and
    dispatches one lead at a time.
    """

    def __init__(self, worker_uuid: Optional[str] = None):
        self._uuid = worker_uuid or f"w-{uuid.uuid4().hex[:12]}"
        self._task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        # Where the current dispatch stands. Past ``_COMMITTED`` a dial may
        # be on the wire, so stop() must wait for it instead of cancelling.
        self._phase = _IDLE
        self._lead_in_flight: Optional[str] = None
        # Template of the lead being dispatched (a worker handles one lead at a time).
        self._current_template_id: Optional[str] = None
        # The v2 line held by the lead being dispatched, if it came from the acceptor.
        self._v2_line: Optional[_DispatchLine] = None

    @property
    def uuid(self) -> str:
        return self._uuid

    @property
    def v2_cancel_safe(self) -> bool:
        """Whether a stopping acceptor may cancel this held-line dispatch: it is in its
        checks or its greeting wait, which give the line back on a cancel. Not before
        them (its claim, which a cancel could leave owned by nobody), nor from the
        line's commit on."""
        return self._phase in (_PRE_DIAL, _PREWARM)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"bb-worker-hb-{self._uuid}"
        )
        self._task = asyncio.create_task(self._loop(), name=f"bb-worker-{self._uuid}")

    async def stop(self) -> None:
        """Stop taking leads, then drain.

        An idle worker (BLPOP) or one still in the pre-dial checks holds
        nothing on the wire and is cancelled at once. A dispatch that has
        reached the commit point (inside or past ``make_call``) is waited
        for, never cancelled: cancelling would leave ``make_call`` running in
        its thread while the ``finally`` unlocks a lead whose call may exist
        (a re-dial) and the token + DB channel leak. The wait is bounded by
        ``BB_WORKER_SHUTDOWN_DRAIN_S``; past it we stop waiting but still do
        not cancel.
        """
        self._stopping.set()
        task = self._task
        if task is not None:
            deadline = asyncio.get_running_loop().time() + BB_WORKER_SHUTDOWN_DRAIN_S
            while not task.done():
                if self._phase in _CANCELLABLE:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    logger.error(
                        f"Worker {self._uuid}: dial for lead "
                        f"{self._lead_in_flight} still in flight after "
                        f"{BB_WORKER_SHUTDOWN_DRAIN_S}s of shutdown drain; "
                        "NOT cancelling it (its call may exist)"
                    )
                    break
                # Re-check the phase often: a dispatch that ends (or a worker
                # that never committed) must not wait out the full budget.
                await asyncio.wait({task}, timeout=min(remaining, 0.25))
            self._task = None
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    # -- heartbeat ----------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """
        Refresh ``bb:worker:heartbeat:{uuid}`` periodically so the reaper
        knows we're alive. Best-effort; if Redis is down, the reaper will
        treat us as dead — which is the correct conservative behaviour.
        """
        key = worker_heartbeat_key(self._uuid)
        # Keep beating while a committed dial drains after stop(): a silent
        # heartbeat would let the reaper requeue the lead that is mid-dial.
        while not self._stopping.is_set() or self._phase not in _CANCELLABLE:
            try:
                redis = await get_redis_service()
                await redis.setex(key, "1", ttl_seconds=BB_WORKER_HEARTBEAT_TTL_S)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Worker {self._uuid} heartbeat write failed: {e}")
            try:
                if self._stopping.is_set():
                    await asyncio.sleep(BB_WORKER_HEARTBEAT_REFRESH_S)
                else:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=BB_WORKER_HEARTBEAT_REFRESH_S
                    )
            except asyncio.TimeoutError:
                pass

    # -- runtime guards -----------------------------------------------------

    async def _dispatch_globally_disabled(self) -> bool:
        """Read the dispatcher kill-switch from dynamic config (DevCycle/Redis).
        Fails open (returns False = enabled) if the config layer is unreachable
        so a sick Redis can't silently halt dispatch.
        """
        try:
            return not await dyn_cfg.BB_DISPATCH_ENABLED()
        except Exception:  # noqa: BLE001
            return False

    async def _reseller_paused(self, reseller_id: str) -> bool:
        try:
            redis = await get_redis_service()
            return await redis.exists(reseller_paused_key(reseller_id))
        except Exception:  # noqa: BLE001
            return False

    # -- main loop ----------------------------------------------------------

    async def _loop(self) -> None:
        async with create_aiohttp_session() as session:
            while not self._stopping.is_set():
                try:
                    await self._iteration(session)
                except Exception as e:  # noqa: BLE001
                    logger.error(
                        f"Worker {self._uuid}: unexpected error in loop: {e}",
                        exc_info=True,
                    )
                    # Yield briefly so we don't hot-spin if something is
                    # systemically broken.
                    await asyncio.sleep(1.0)

    async def _iteration(self, session: Optional[aiohttp.ClientSession]) -> None:
        """One pop-and-dispatch cycle. Errors are logged and contained."""
        if await self._dispatch_globally_disabled():
            await asyncio.sleep(2.0)
            return

        lead_id = await self._blpop_ready()
        if lead_id is None:
            return  # timeout — loop back so we can check stop signal

        # No await between the pop and this line: a stop() that sees
        # _PRE_DIAL may cancel, one that sees _IDLE found nothing in flight.
        self._phase = _PRE_DIAL
        self._lead_in_flight = lead_id
        try:
            # Track in-flight for crash recovery. Inside the try: a stop() that
            # cancels during this RPUSH (the phase is already cancellable) must
            # still put the popped lead back on the schedule below.
            await self._rpush_processing(lead_id)
            await self._dispatch(lead_id, session)
        except asyncio.CancelledError:
            # stop() cancelled a dispatch that held nothing (see _CANCELLABLE; the
            # pre-warm gives its channel back itself). The lead was popped off the
            # ready list and its lock is released, so put it back on the schedule:
            # nothing else would soon (the backlog reconciler sees only the oldest
            # due rows, so under a pile it could wait for hours).
            await schedule_lead(lead_id, datetime.now(timezone.utc))
            raise
        finally:
            await self._lrem_processing(lead_id)
            self._phase = _IDLE
            self._lead_in_flight = None

    async def _blpop_ready(self) -> Optional[str]:
        try:
            redis = await get_redis_service()
            client: Any = cast(Any, await redis.get_client())
            popped = await client.blpop(READY_LIST, timeout=BB_WORKER_BLPOP_TIMEOUT_S)
            if popped is None:
                return None
            _, lead_id = popped
            return lead_id
        except Exception as e:  # noqa: BLE001
            logger.error(f"Worker {self._uuid}: BLPOP ready failed: {e}")
            await asyncio.sleep(1.0)
            return None

    async def _rpush_processing(self, lead_id: str) -> None:
        try:
            redis = await get_redis_service()
            client: Any = cast(Any, await redis.get_client())
            await client.rpush(processing_list_for(self._uuid), lead_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Worker {self._uuid}: RPUSH processing failed for {lead_id}: {e}. "
                "Reaper recovery degraded for this lead."
            )

    async def _lrem_processing(self, lead_id: str) -> None:
        try:
            redis = await get_redis_service()
            client: Any = cast(Any, await redis.get_client())
            await client.lrem(processing_list_for(self._uuid), 1, lead_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Worker {self._uuid}: LREM processing failed: {e}")

    # -- dispatch -----------------------------------------------------------

    async def _dispatch(
        self,
        lead_id: str,
        session: Optional[aiohttp.ClientSession],
        held: Optional[ClaimedTicket] = None,
    ) -> bool:
        """
        Dispatch one lead. ``held`` is the v2 line an acceptor coroutine holds
        for it (design card §4); without it the lead takes today's channel
        token + DB channel. Returns True only if a call was placed.

        Any exit that did not place a call gives a held v2 line back, so no
        path can leave one held (a no-op once the lease isn't this ticket's).
        """
        self._v2_line = (
            _DispatchLine(ticket=held, lead_id=lead_id) if held is not None else None
        )
        if held is not None:
            self._phase = _PRE_DIAL  # as _iteration does for today's worker
        self._current_template_id = None
        dialled = False
        try:
            dialled = await self._dispatch_lead(lead_id, session, held)
            return dialled
        finally:
            if not dialled:
                await self._give_back_v2()

    async def _give_back_v2(self) -> None:
        """Give the held v2 line back. Before any ``schedule_lead`` of the current
        lead: ``enqueue`` skips a lead that still holds a line (design card rule 17)."""
        if self._v2_line is not None:
            await self._v2_line.give_back()

    async def _lead_is_ours(self) -> bool:
        """True on today's path, or while this v2 dispatch's ticket still owns the lead.
        The held line is given back first (a no-op once given, or while it is the
        call's): a lease no longer this ticket's was reaped or re-issued, and the lead's
        lock and schedule are its new holder's, so this dispatch must not unlock, defer,
        finish or re-queue it."""
        if self._v2_line is None:
            return True
        await self._v2_line.give_back()
        return not self._v2_line.not_ours

    async def _schedule_if_ours(
        self, lead_id: str, when: datetime, **kwargs: Any
    ) -> None:
        if await self._lead_is_ours():
            await schedule_lead(lead_id, when, **kwargs)

    async def _dispatch_lead(
        self,
        lead_id: str,
        session: Optional[aiohttp.ClientSession],
        held: Optional[ClaimedTicket],
    ) -> bool:
        """
        Run the full dispatch flow for one lead. All exit paths leave the
        lead row in a consistent state (lock released, status correct).
        """
        logger.info(f"Worker {self._uuid}: picked lead {lead_id}")

        lead = await get_lead_by_id(lead_id)
        if not lead:
            logger.warning(f"Worker {self._uuid}: lead {lead_id} not found in DB")
            return False
        if lead.status != LeadCallStatus.BACKLOG:
            logger.info(
                f"Worker {self._uuid}: lead {lead_id} status is "
                f"{lead.status.value}, skipping"
            )
            return False
        if not is_dispatchable(lead.execution_mode):
            # Defensive backstop. The ingest paths (handler, retry,
            # dispatch-now) and the reconciler query all filter non-
            # dispatchable modes out, so this should never fire. If it
            # does, drop the lead with a loud log rather than dialling
            # a phantom PSTN call for a DAILY/web-mode lead.
            logger.error(
                f"Worker {self._uuid}: lead {lead_id} has non-dispatchable "
                f"execution_mode={lead.execution_mode.value}; dropping without "
                "dispatch (someone bypassed the schedule_lead gate)."
            )
            return False
        if await self._reseller_paused(lead.reseller_id):
            logger.info(
                f"Worker {self._uuid}: reseller {lead.reseller_id} paused, "
                f"re-scheduling lead {lead_id}"
            )
            # Defer 30s; operator unpauses by removing the key.
            await self._schedule_if_ours(
                lead_id,
                datetime.now(timezone.utc) + timedelta(seconds=30),
                template_id=lead.template_id,
            )
            return False

        locked = await acquire_lock_on_lead_by_id(
            lead_id, expected_status=LeadCallStatus.BACKLOG
        )
        if not locked:
            logger.info(
                f"Worker {self._uuid}: lead {lead_id} could not be locked "
                "(another worker or status changed)."
                + (" Giving the held line back." if held is not None else " Dropping.")
            )
            if held is not None:
                # A re-issued ticket whose lock a stale ticket's task still holds:
                # give the line back, and re-queue (30 s out) only a lead that is
                # still BACKLOG, so a finished or dialling lead is never looped.
                if not await self._lead_is_ours():
                    return False
                again = await get_lead_by_id(lead_id)
                if again is not None and again.status == LeadCallStatus.BACKLOG:
                    # Never earlier than the DB's next_attempt_at.
                    retry_at = datetime.now(timezone.utc) + timedelta(
                        seconds=V2_LOCK_RETRY_S
                    )
                    if again.next_attempt_at is not None:
                        retry_at = max(again.next_attempt_at, retry_at)
                    await schedule_lead(lead_id, retry_at, template_id=lead.template_id)
            return False

        self._current_template_id = locked.template_id
        if (
            held is not None  # v2 only: today's worker is left as it was
            and locked.next_attempt_at is not None
            and locked.next_attempt_at
            > datetime.now(timezone.utc) + timedelta(seconds=EARLY_PICK_TOLERANCE_S)
        ):
            # A stale queue copy (e.g. a backlog page read before this lead was
            # deferred) picked it before its due time: never dial early. The row
            # just locked is the truth; put the lead back at its own time.
            logger.info(
                f"Worker {self._uuid}: lead {lead_id} picked before its due time "
                f"{locked.next_attempt_at.isoformat()}; re-scheduled, not dialled"
            )
            if await self._release(locked.id) and await self._lead_is_ours():
                await schedule_lead(
                    locked.id, locked.next_attempt_at, template_id=locked.template_id
                )
            return False
        lock_released = False
        # held: per-template reads come from the pod's memo (see _DIAL_MEMO)
        memo_tid = locked.template_id if held is not None else None
        try:
            config = await _dial_read(
                memo_tid,
                ("cfg", memo_tid, locked.template),
                lambda: _get_lead_config(locked),
            )
            if not config:
                lock_released = await self._fail_and_release(locked.id, "NO_CONFIG")
                return False

            if not config.enable_calling:
                logger.info(f"Worker {self._uuid}: calling disabled for lead {lead_id}")
                lock_released = await self._release(locked.id)
                return False

            customer_phone = (locked.payload or {}).get("customer_mobile_number")
            if customer_phone and await is_number_blacklisted(
                customer_phone, locked.reseller_id
            ):
                if await self._lead_is_ours():
                    await update_lead_call_completion_details(
                        id=locked.id,
                        status=LeadCallStatus.FINISHED,
                        outcome="BLACKLISTED",
                        meta_data={"reason": "Phone number is blacklisted"},
                        call_end_time=datetime.now(timezone.utc),
                    )
                lock_released = await self._release(locked.id)
                return False

            if not _is_within_calling_hours(config):
                # Defer until window opens — we approximate by deferring 5 min
                # and letting the reconciler/promoter re-pick. Cheaper than
                # computing the exact next window here.
                lock_released = await self._defer_and_release(locked.id, 300)
                return False

            # id-only resolution: leads always carry the template_id they
            # resolved to at push time; name fallback was removed.
            template = await _dial_read(
                memo_tid,
                ("tpl", memo_tid),
                lambda: _template_by_id(locked.template_id),
            )
            if not template:
                logger.error(
                    f"Worker {self._uuid}: no template found for lead "
                    f"{locked.id} (template_id={locked.template_id}, "
                    f"reseller={locked.reseller_id}). Proceeding to dial "
                    "without a template."
                )

            pre_check_decision, pre_check_defer = await _run_pre_checks_for_lead(
                config, locked, template, session
            )
            if pre_check_decision is PreCheckDecision.ABORT:
                # _run_pre_checks_for_lead already set status to FINISHED on
                # failure; we just need to release the lock.
                lock_released = await self._release(locked.id)
                return False
            if pre_check_decision is PreCheckDecision.DEFER:
                # Transient block (cooldown, quota). Lead stays BACKLOG and
                # comes back after pre_check_defer seconds.
                lock_released = await self._defer_and_release(
                    locked.id, pre_check_defer
                )
                return False

            if template:
                template = apply_playground_overrides(locked, template)

            # Rate-limit PEEK before channel token. Read-only — we don't
            # record the attempt here, because we may still bail downstream
            # (channel-token exhaustion, provider error) without ever dialing.
            # The matching record_outbound_call_attempt() runs only after
            # provider.make_call succeeds. Inactive templates skip the
            # rate limit entirely (per release fix 2cd510d).
            rate_limited_phone = (
                locked.execution_mode == ExecutionMode.TELEPHONY
                and customer_phone
                and (template is None or getattr(template, "is_active", True))
            )
            if rate_limited_phone:
                rate_ok, defer_seconds = await peek_outbound_rate_limit_and_alert(
                    customer_phone=cast(str, customer_phone),
                    lead_id=str(locked.id),
                    reseller_id=locked.reseller_id,
                )
                if not rate_ok:
                    lock_released = await self._defer_and_release(
                        locked.id, defer_seconds
                    )
                    return False

            # The merchant's per-customer call rule (ADR 0025) — PEEK, before
            # a channel token is held, so a customer already at the limit
            # burns no capacity. Every customer dial is asked: workflow call
            # squares, push-API leads, campaigns and the agent's own re-dials.
            # Inactive templates are NOT exempt (unlike the hourly limiter): a
            # customer limit that changes with the calling template is not a
            # customer limit. Nor are test or playground dials:
            # TELEPHONY_TEST and is_playground are fields the merchant sends
            # on its own push, and both still ring the real customer number —
            # an exemption keyed on them would let the merchant this cap
            # constrains switch it off. The cap protects the phone, so every
            # PSTN dial to it counts; a merchant testing on its own phone
            # counts against its own cap.
            call_limits = None
            call_limit_member = None
            if (
                locked.execution_mode
                in (ExecutionMode.TELEPHONY, ExecutionMode.TELEPHONY_TEST)
                and locked.merchant_id
                and isinstance(customer_phone, str)
                and customer_phone
            ):
                try:
                    call_limits = await merchant_call_limits(locked.merchant_id)
                    if call_limits:
                        verdict = await peek_call_limit(
                            merchant_id=locked.merchant_id,
                            phone=customer_phone,
                            lead_id=str(locked.id),
                            rules=call_limits,
                        )
                        if not verdict.allowed:
                            await finish_lead_call_limit_reached(
                                locked, verdict, session
                            )
                            lock_released = await self._release(locked.id)
                            return False
                except CallLimitUnavailable as e:
                    lock_released = await self._defer_call_limit_unavailable(
                        locked.id, e
                    )
                    return False

            number = await _dial_read(
                memo_tid,
                ("num", memo_tid),
                lambda: _get_available_number(config, template),
            )
            if not number:
                # Permanent / semi-permanent failure: misconfigured template
                # or no number in the fallback pool. Retrying every 10s would
                # be a hot loop on an unresolvable state — instead, mark the
                # lead FINISHED with a terminal outcome and alert ops.
                logger.error(
                    f"Worker {self._uuid}: no telephony number for lead "
                    f"{locked.id} (template={config.template}, "
                    f"reseller={config.reseller_id}, merchant={config.merchant_id}). "
                    "Marking FINISHED with NUMBER_UNAVAILABLE."
                )
                try:
                    await raise_no_telephony_number(
                        reseller_id=config.reseller_id,
                        template=config.template,
                        merchant_id=config.merchant_id,
                    )
                except Exception as alert_exc:  # noqa: BLE001
                    logger.warning(
                        f"Worker {self._uuid}: raise_no_telephony_number "
                        f"failed for lead {locked.id}: {alert_exc}"
                    )
                lock_released = await self._fail_and_release(
                    locked.id, "NUMBER_UNAVAILABLE"
                )
                return False

            if held is None:
                owned = await v2_owns_number(str(number.id))
                if owned is not False:
                    # v2 owns this number or is switching it (or its mode can't be
                    # read, or Redis lost v2's state): today's worker never dials on it
                    # (design card rule 21).
                    lock_released = await self._redirect_to_v2(locked, owned)
                    return False

            call_provider = get_voice_provider(
                number.provider, session, config.telephony_config
            )
            # Dial on the account the template names (its
            # telephony_configuration), as the live call does — before any
            # capacity is taken, so no exit here holds a token.
            try:
                await call_provider.use_template_credentials(
                    Accounts(locked.reseller_id, locked.merchant_id),
                    getattr(template, "configurations", None),
                )
            except AccountRefused as e:
                logger.error(
                    f"Worker {self._uuid}: telephony account refused for lead "
                    f"{locked.id}: {e}. Marking FINISHED with NUMBER_UNAVAILABLE."
                )
                lock_released = await self._fail_and_release(
                    locked.id, "NUMBER_UNAVAILABLE"
                )
                return False

            # From here a channel is (being) taken: stop() waits instead of
            # cancelling, so a cancel can never land between taking it and the
            # explicit give-back paths below.
            self._phase = _HOLDING
            if held is not None:
                if str(number.id) != held.number_id:
                    # The template's number changed while the ticket waited: give
                    # the old line back and re-queue on the current route (design
                    # card §6 rule 1). The memo may be what still names the old
                    # number: drop it first, or every ticket on the new route would
                    # bounce here until it expires.
                    await self._give_back_v2()
                    _forget_dial_memo(locked)
                    await _invalidate_route(locked.template_id)
                    lock_released = await self._release(locked.id)
                    await self._schedule_if_ours(
                        locked.id,
                        datetime.now(timezone.utc),
                        jitter_ms=0,
                        template_id=locked.template_id,
                    )
                    return False
                # The coroutine already holds the line (token and DB channel are
                # v2's busy list), so today's gates are skipped.
                line = cast(_DispatchLine, self._v2_line)
            else:
                # Channel token gate (Redis). Held until call-end webhook releases.
                token = await acquire_channel_token(number.id)
                if token is None:
                    # No capacity right now — re-schedule (see
                    # capacity_defer_seconds for the delay).
                    lock_released = await self._defer_and_release(
                        locked.id, await capacity_defer_seconds(number.id, locked.id)
                    )
                    return False

                # DB-side bookkeeping: ``telephony_number.status`` (Twilio) or
                # ``channels`` (Exotel/Plivo). The ``+1 WHERE channels < max`` is
                # atomic, so a refusal means the token matched no free line.
                acquired_db = await _acquire_number(number)
                if not acquired_db:
                    # Drop the token rather than push it back: pushed back, the
                    # next worker pops the same stale token and is refused
                    # again. The line returns via the call-end release, and the
                    # reconciler tops the list up if the token was real.
                    logger.warning(
                        f"Worker {self._uuid}: DB capacity denied for number "
                        f"{number.id} despite Redis token. Dropping token, deferring."
                    )
                    lock_released = await self._defer_and_release(
                        locked.id, await capacity_defer_seconds(number.id, locked.id)
                    )
                    return False
                line = _DispatchLine(number=number, token=token)

            customer_mobile = (locked.payload or {}).get("customer_mobile_number")
            if not customer_mobile or not isinstance(customer_mobile, str):
                logger.error(
                    f"Worker {self._uuid}: invalid customer_mobile_number "
                    f"for lead {locked.id}"
                )
                await line.give_back()
                lock_released = await self._fail_and_release(locked.id, "INVALID_PHONE")
                return False

            # Atomic check-and-record — the authoritative cap. Placement
            # constraints (see record_outbound_call_attempt docstring):
            #   1. After acquire_channel_token + _acquire_number, so a
            #      channel-token-exhaustion retry loop can't ZADD on every
            #      bounce (the pre-PR-#776 self-fill bug).
            #   2. Before make_call, so we can still bail when the atomic
            #      Lua detects a cross-lead race — once make_call is on the
            #      wire, strict cap is meaningless (you can't un-dial).
            # If rejected, release the channel token + DB number and defer
            # by the rate-limit window so the dispatcher doesn't immediately
            # re-pick this lead and burn through the next window of attempts.
            if rate_limited_phone:
                rl_ok, rl_defer = await record_outbound_call_attempt(
                    customer_phone=cast(str, customer_phone),
                    lead_id=str(locked.id),
                    reseller_id=locked.reseller_id,
                )
                if not rl_ok:
                    logger.warning(
                        f"Worker {self._uuid}: rate-limit race rejected lead "
                        f"{locked.id} at atomic record (another worker on the "
                        f"same phone filled the bucket between our peek and "
                        f"record). Releasing channel token + number, "
                        f"deferring {rl_defer}s."
                    )
                    await line.give_back()
                    lock_released = await self._defer_and_release(locked.id, rl_defer)
                    return False

            # Greeting pre-warm as late as possible — after every gate,
            # immediately before the dial — so TTS/generation spend is
            # proportional to actual dials, not dispatch attempts. The
            # channel token acquired above is held during the bounded
            # wait (by design: dials pacing to generation capacity under
            # degradation). Bounded and fail-open — a slow or hung
            # generator never blocks the dial; the answer-time path
            # retries synthesis as a cache miss.
            if template:
                self._phase = _PREWARM  # long (up to ~60 s) and cancel-safe
                try:
                    if held is None:
                        await _prewarm_initial_greeting_with_retry(
                            lead_id=locked.id,
                            payload=locked.payload or {},
                            template=template,
                        )
                    else:
                        await _prewarm_beside_the_dial(
                            locked.id, locked.payload or {}, template
                        )
                except asyncio.CancelledError:
                    # Cancellation (worker shutdown) mid-prewarm: none of
                    # the explicit release paths below ran, and the outer
                    # finally only releases the lead lock. Return the
                    # channel token + DB number, then re-raise so the
                    # cancellation propagates.
                    logger.warning(
                        f"Worker {self._uuid}: cancelled during greeting "
                        f"prewarm for lead {locked.id}; releasing channel "
                        "token + number"
                    )
                    await line.give_back()
                    raise
                self._phase = _HOLDING

            # Commit point for shutdown: from here a dial may reach the wire,
            # so Worker.stop() drains this dispatch instead of cancelling it.
            self._phase = _COMMITTED

            # The merchant's per-customer rule — the authoritative RECORD,
            # atomic with its count. The LAST step before the phone rings:
            # after the greeting pre-warm (up to ~60s on Gemini Live), so a
            # worker stopped or killed mid-pre-warm never leaves an entry for
            # a dial that did not happen. Still after the hourly limiter's
            # record: an hourly deferral never lands in the merchant's window,
            # while a refusal here is terminal, so the hourly bucket's extra
            # entry happens at most once per lead. Every record counts (ADR
            # 0025 §3) — except one the provider says it did not place, which
            # is taken back below.
            if call_limits and locked.merchant_id:
                try:
                    verdict = await record_call_limit(
                        merchant_id=locked.merchant_id,
                        phone=customer_mobile,
                        lead_id=str(locked.id),
                        rules=call_limits,
                    )
                except CallLimitUnavailable as e:
                    await line.give_back()
                    lock_released = await self._defer_call_limit_unavailable(
                        locked.id, e
                    )
                    return False
                if not verdict.allowed:
                    # Lost the race to another worker dialling the same
                    # customer, or the window filled since the peek.
                    await line.give_back()
                    if await self._lead_is_ours():
                        await finish_lead_call_limit_reached(locked, verdict, session)
                    lock_released = await self._release(locked.id)
                    return False
                call_limit_member = verdict.member

            if held is not None:
                mark = await v2_scripts.mark_dialling(
                    held.number_id, str(locked.id), held.tk, held.owner
                )
                if mark is not v2_scripts.Mark.DIAL:
                    # The line is not provably ours, so never dial (rule 15).
                    await self._unrecord_call_limit(
                        locked, customer_mobile, call_limit_member
                    )
                    if mark is v2_scripts.Mark.SUPERSEDED:
                        # The reaper freed our line while the checks ran, unlocked the
                        # lead and re-issued it: the newer ticket's coroutine may hold
                        # the lock now. Neither the lock nor a re-queue is ours.
                        lock_released = True
                        return False
                    # No lease left, the kill switch, or Redis failed: line back (refused
                    # if the lease was in fact marked: the stuck-dial reap bounds it),
                    # unlock, and the lead back in its room now.
                    await line.give_back()
                    lock_released = await self._release(locked.id)
                    await self._schedule_if_ours(
                        locked.id,
                        datetime.now(timezone.utc),
                        jitter_ms=0,
                        template_id=locked.template_id,
                    )
                    return False
                line.dialling = True

            # The provider echoes these back on the call's webhooks — the only
            # link to this lead if its reply (with the call id) never arrives.
            # dial_at is also the call_initiated_time an unknown dial is held
            # under, so a webhook matches this exact dial and no other.
            dialled_at = datetime.now(timezone.utc)
            dial_ref = {"lead_id": str(locked.id), "dial_at": dial_ref_time(dialled_at)}
            unknown_marker = {
                UNKNOWN_DIAL_META_KEY: {
                    "dial_at": dial_ref["dial_at"],
                    "call_limit_member": call_limit_member,
                }
            }

            # Plivo: write the dial's row BEFORE the request (PROCESSING, no call
            # id, call_initiated_time = dial_at, the unknown-dial marker), so a
            # webhook that beats Plivo's reply (an answer or hangup inside our
            # read timeout, or after a slow reply) claims this lead by its
            # dial_ref instead of matching nothing. The reply then only stamps
            # the call id; a lost reply needs nothing more (the row is the hold).
            premarked = number.provider == CallProvider.PLIVO
            if premarked and not await hold_unknown_dial(
                locked.id, dialled_at, number.id, unknown_marker
            ):
                # The row left BACKLOG before we dialled (the merchant finished
                # it): nothing rang.
                await self._unrecord_call_limit(
                    locked, customer_mobile, call_limit_member
                )
                await line.give_back(not_placed=True)
                lock_released = await self._release(locked.id)
                return False

            try:
                if held is None:
                    call = await call_provider.make_call_async(
                        customer_mobile,
                        number.number,
                        reseller_id=locked.reseller_id,
                        # answer-url observability tag only (never parsed back);
                        # id-only convention — no template names in routing.
                        template_name=locked.template_id or "",
                        dial_ref=dial_ref,
                    )
                else:
                    call = await self._dial_held(
                        call_provider, customer_mobile, number, locked, dial_ref, held
                    )
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"Worker {self._uuid}: provider.make_call failed for "
                    f"lead {locked.id}: {e}"
                )
                # Raised before any provider reply: nothing was placed.
                await self._unrecord_call_limit(
                    locked, customer_mobile, call_limit_member
                )
                # Backoff retry. Use defer_seconds derived from attempt_count.
                backoff = min(60, 5 * (locked.attempt_count + 1))
                lock_released = await self._not_placed(
                    locked, line, dialled_at, backoff, premarked
                )
                return False

            if call and call.get("status") == DIAL_OUTCOME_UNKNOWN:
                # Sent, but no reply: the provider may have placed the call,
                # so this is neither a failure nor a dial to repeat. Hold the
                # lead exactly as a placed call — PROCESSING, lock + channel
                # + call-limit record kept — just without a call_id. The
                # provider's webhook fills it in (claim_unknown_dial) and the
                # call runs its normal course; if no webhook ever comes, the
                # provider never placed it, and the stuck-PROCESSING sweep puts
                # this same lead back to be dialled (requeue_unclaimed_unknown_dial)
                # after releasing the channel once and taking back the
                # call-limit entry recorded here. A v2 line is kept the same way:
                # the lease is marked dialling, so returning True clears the lease
                # and the busy holder lead:<id> stays until the call ends. A Plivo
                # dial's row was written before the request: it already is the hold.
                on_hold = premarked or await hold_unknown_dial(
                    locked.id, dialled_at, number.id, unknown_marker
                )
                if not on_hold:
                    # Same as the CAS loss below: the row moved on under us.
                    logger.error(
                        f"Worker {self._uuid}: dial outcome unknown and CAS "
                        f"lost for lead {locked.id}. Releasing resources; the "
                        "call may be orphaned."
                    )
                    if held is None:  # a v2 line belongs to the call that may exist
                        await line.give_back()
                    lock_released = await self._release(locked.id)
                    return True
                lock_released = True
                logger.warning(
                    f"Worker {self._uuid}: dial outcome unknown for lead "
                    f"{locked.id} via {number.provider.value} number "
                    f"{number.id}; held PROCESSING until the provider's "
                    "webhook claims it or the stuck sweep closes it"
                )
                return True  # the line (today's channel, or a marked v2 lease) is the call's

            if not call or not call.get("sid"):
                logger.error(
                    f"Worker {self._uuid}: provider.make_call returned no SID "
                    f"for lead {locked.id}: {call}"
                )
                if call is None:
                    # Every adapter maps its own failure (4xx/5xx/429, a
                    # client error) to None: the provider did not place the
                    # call, so this dial never rang. A reply WITHOUT a SID
                    # (Exotel's empty 2xx) may have rung — its count stays.
                    await self._unrecord_call_limit(
                        locked, customer_mobile, call_limit_member
                    )
                # A SID-less reply may have rung, so a v2 line stays held (the
                # give-back is refused); a None reply is "not placed".
                lock_released = await self._not_placed(
                    locked, line, dialled_at, 10, premarked, not_placed=call is None
                )
                return False

            call_sid = str(call.get("sid"))

            # Keyed on the SID, so a retried lead records a second attempt
            # rather than colliding with the first. Test/playground traffic
            # never reaches the CRM.
            if not is_non_customer_lead(locked.execution_mode, locked.metaData):
                spawn_background_task(
                    mirror_to_crm(
                        "call.attempted",
                        merchant_id=locked.merchant_id,
                        external_id=call_sid,
                        lead_id=str(locked.id),
                        phone=customer_mobile,
                        # Pass-through of the creation-time stamp; mirrors
                        # never resolve.
                        customer_id=locked.customer_id,
                        call_id=call_sid,
                        attempt_count=locked.attempt_count,
                        template_id=locked.template_id,
                        direction="OUTBOUND",
                    ),
                    name=f"crm-call-attempted-{call_sid}",
                )

            if premarked:
                call_initiated_time = dialled_at
                updated = await stamp_dialled_call(
                    locked.id, dialled_at, call_sid, UNKNOWN_DIAL_META_KEY
                )
                if not updated:
                    current = await get_lead_by_id(str(locked.id))
                    if current is not None and str(current.call_id) == call_sid:
                        # Its webhooks claimed it and the call already ended:
                        # the end webhook released the line and the lock.
                        lock_released = True
                        return True
            else:
                call_initiated_time = datetime.now(timezone.utc)
                updated = await update_lead_call_details(
                    locked.id,
                    LeadCallStatus.PROCESSING,
                    call_sid,
                    call_initiated_time,
                    number.id,
                )
            if not updated:
                # CAS lost. Two very different causes:
                #  (a) the lead really left BACKLOG (the merchant aborted it
                #      during the dial): the call is LIVE and holds this
                #      channel, so the line belongs to the call. Stamp the
                #      call on the lead (status untouched, marker merged into
                #      meta_data) so its end webhook finds it by call id and
                #      returns the line once. Nothing is given back here: the
                #      DB channel is the real guard (the Redis token is
                #      re-minted by reconcile_channel_tokens within 60 s).
                #  (b) update_lead_call_details swallowed a DB error and the
                #      row is still BACKLOG: stamping + unlocking would let the
                #      lead be re-dialled and lose this call's line, so the
                #      attach refuses (it requires FINISHED) and we fall back
                #      to releasing the line.
                stamped = await attach_placed_call_to_lead(
                    locked.id, call_sid, call_initiated_time, number.id
                )
                if stamped:
                    logger.error(
                        f"Worker {self._uuid}: post-make_call CAS lost for "
                        f"lead {locked.id} (call_sid={call_sid}); the lead is "
                        "finished but the call is live: keeping its line, "
                        "stamped the call on the lead so its end webhook "
                        "releases the line."
                    )
                else:
                    logger.error(
                        f"Worker {self._uuid}: post-make_call CAS lost for "
                        f"lead {locked.id} (call_sid={call_sid}) and the lead "
                        "is not a finished one that can carry the call. "
                        "Releasing the line; the call may be orphaned."
                    )
                    if held is None:
                        # A v2 line belongs to the placed call (its id), not to
                        # this ticket: it is not given back here.
                        await line.give_back()
                lock_released = await self._release(locked.id)
                return True

            # Success — token stays held until call-end webhook fires.
            # Lock stays held too; the call-end webhook releases it. Setting
            # lock_released so the defensive finally below doesn't unlock the
            # row out from under the webhook handler.
            lock_released = True
            logger.info(
                f"Worker {self._uuid}: dialled lead {locked.id} via "
                f"{number.provider.value} number {number.id} (call_sid={call_sid})"
            )
            return True
        finally:
            if not lock_released:
                # Defensive: any path that didn't already release the lock
                # must release here.
                try:
                    await release_lock_on_lead_by_id(locked.id)
                except Exception as e:  # noqa: BLE001
                    logger.error(
                        f"Worker {self._uuid}: final lock release failed "
                        f"for {locked.id}: {e}"
                    )

    # -- exit helpers -------------------------------------------------------

    async def _redirect_to_v2(self, lead: Any, owned: Optional[bool]) -> bool:
        """Hand a lead on a v2-accounted number back to its v2 room, due as before,
        without dialling. If its mode can't be read (or Redis lost v2's state), or it
        can't go to a room (its template's route can't be resolved), defer it instead of
        bouncing it in a loop.
        """
        if owned and not lead.template_id:
            # no room to put it in (today would dial it "without a template")
            logger.warning(
                f"Worker {self._uuid}: lead {lead.id} has no template_id and its "
                f"number is on v2; deferring {V2_REDIRECT_RETRY_S}s"
            )
        elif owned:
            await self._release(lead.id)  # first, so a ticket for it can lock it
            when = lead.next_attempt_at or datetime.now(timezone.utc)
            if await requeue_in_room(lead.id, when, lead.template_id) is not None:
                logger.info(
                    f"Worker {self._uuid}: lead {lead.id} is on a v2 number; "
                    "back to its v2 room"
                )
                return True
        return await self._defer_and_release(lead.id, V2_REDIRECT_RETRY_S)

    async def _release(self, lead_id: str) -> bool:
        """Unlock the lead. True also when there is nothing of ours to unlock (a v2
        ticket that no longer owns the lead: see ``_lead_is_ours``)."""
        if not await self._lead_is_ours():
            return True
        try:
            await release_lock_on_lead_by_id(lead_id)
        except Exception as e:  # noqa: BLE001
            logger.error(f"release_lock failed for {lead_id}: {e}")
        return True

    async def _unrecord_call_limit(
        self, lead: Any, phone: str, member: Optional[str]
    ) -> None:
        """Take back this dial's call-limit entry (best effort) when the
        provider says the call was not placed."""
        if member and lead.merchant_id:
            await unrecord_call_limit(
                merchant_id=lead.merchant_id, phone=phone, member=member
            )

    async def _defer_call_limit_unavailable(
        self, lead_id: str, error: CallLimitUnavailable
    ) -> bool:
        """Fail CLOSED on the merchant's call rule (ADR 0025 §6): when it
        cannot be read or evaluated, do not dial — defer briefly and ask
        again. A transient, not a verdict: the lead stays BACKLOG. Costs
        nothing extra, since the queue and the channel tokens live in the
        same Redis; when it is down nothing dials anyway. A throttled P0
        fires only for a merchant KNOWN to have a rule (its rule unreadable,
        its own check failed) — its customer calls stop until someone acts.
        When the rules themselves could not be read, nothing is known about
        the merchant, so it defers and logs without paging."""
        logger.bind(lead_id=lead_id, lead_skip=CALL_LIMIT_UNAVAILABLE_REASON).warning(
            f"Worker {self._uuid}: call limit unavailable for lead {lead_id} "
            f"({error}); deferring {CALL_LIMIT_UNAVAILABLE_DEFER_S}s"
        )
        if error.capped:
            try:
                await raise_call_limit_unavailable(str(error))
            except Exception as alert_exc:  # noqa: BLE001 — never blocks
                logger.warning(f"call-limit unavailable alert failed: {alert_exc}")
        return await self._defer_and_release(lead_id, CALL_LIMIT_UNAVAILABLE_DEFER_S)

    async def _not_placed(
        self,
        locked: Any,
        line: "_DispatchLine",
        dialled_at: datetime,
        defer_s: int,
        premarked: bool,
        not_placed: bool = True,
    ) -> bool:
        """The provider placed no call: give the line back and defer the lead.

        ``line`` is today's channel token + DB channel or the v2 ticket's line.
        A dial row written before the request goes back to BACKLOG (deferred,
        unlocked) in one statement, unless a webhook claimed it meanwhile: then
        a call exists after all and keeps its line and lock. ``not_placed=False``
        (a SID-less reply that may have rung) keeps a v2 line held, and a dial
        row written before the request stays as the hold. Returns whether the
        lock is released (or now owned by someone else).
        """
        if not premarked:
            await line.give_back(not_placed=not_placed)
            return await self._defer_and_release(locked.id, defer_s)
        if not not_placed:
            # A reply that may have rung, on a row written before the request: that row
            # (PROCESSING, no call id) is already the hold an unknown outcome gets, so
            # the row, its lock and the line stay; the webhook or the stuck sweep
            # resolves it.
            logger.warning(
                f"Worker {self._uuid}: a reply without a call id for lead {locked.id}; "
                "held PROCESSING as an unknown outcome"
            )
            return True
        if await revert_dial_to_backlog(
            locked.id, dialled_at, defer_s, UNKNOWN_DIAL_META_KEY
        ):
            await line.give_back(not_placed=True)
            # The revert unlocked and deferred the row; put it back on the
            # schedule like _defer_and_release does (the backlog reconcilers
            # only see the oldest due rows).
            await schedule_lead(
                locked.id,
                datetime.now(timezone.utc) + timedelta(seconds=defer_s),
                template_id=self._current_template_id,
            )
            return True
        current = await get_lead_by_id(str(locked.id))
        if current is not None and current.call_id:
            logger.error(
                f"Worker {self._uuid}: provider said not placed for lead "
                f"{locked.id}, but a webhook claimed call {current.call_id}: "
                "keeping its line"
            )
            return True
        # The row moved on without a call (the merchant finished it mid-dial).
        await line.give_back(not_placed=True)
        return await self._release(locked.id)

    async def _dial_held(
        self,
        call_provider: Any,
        customer_mobile: str,
        number: Any,
        locked: Any,
        dial_ref: Dict[str, str],
        held: ClaimedTicket,
    ) -> Optional[Dict[str, Any]]:
        """The v2 dial (spec §10.3, §10.6): one request through the provider (the Plivo
        SDK, sent once), asking for a 429 back; after a 429, the same request (same
        dial_ref, same dial row) for at most BB_V2_THROTTLE_MAX_WAIT_S. A 429 proves
        nothing was placed, so giving up returns None: today's not-placed path, once."""

        async def one_request() -> Optional[Dict[str, Any]]:
            return await call_provider.make_call_async(
                customer_mobile,
                number.number,
                reseller_id=locked.reseller_id,
                template_name=locked.template_id or "",
                dial_ref=dial_ref,
                report_throttle=True,
            )

        return await _THROTTLE.dial_until_not_throttled(
            one_request, str(locked.id), held.stopping
        )

    async def _defer_and_release(self, lead_id: str, defer_seconds: int) -> bool:
        """Defer next_attempt_at in DB, ZADD onto the schedule, release lock.

        DB write is authoritative — we only mirror the deferral in Redis when
        the DB write actually moved next_attempt_at. The DB query uses
        ``GREATEST(COALESCE(next_attempt_at, NOW()), NOW() + defer)`` so the
        actual stored value may be further out than ``now + defer_seconds``
        (e.g., when the row was already deferred to a later time). Use the
        DB-returned timestamp for the ZADD so the promoter doesn't fire
        earlier than the DB schedule.
        """
        if not await self._lead_is_ours():
            return True
        deferred = None
        try:
            deferred = await defer_lead_next_attempt_and_release_lock(
                lead_id, defer_seconds
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"defer_lead_next_attempt_and_release_lock failed: {e}")

        if deferred is None:
            # DB defer didn't take effect (accessor returned None or raised).
            # Fall back to a plain unlock and let reconcile_backlog_to_zset
            # re-schedule on its next tick.
            try:
                await release_lock_on_lead_by_id(lead_id)
            except Exception:  # noqa: BLE001
                pass
            return True

        # DB defer succeeded — mirror the DB-authoritative timestamp in Redis.
        # Fall back to now+defer_seconds only if the DB column was somehow
        # NULL (shouldn't happen post-defer, but defensive).
        next_at = deferred.next_attempt_at or (
            datetime.now(timezone.utc) + timedelta(seconds=defer_seconds)
        )
        await schedule_lead(lead_id, next_at, template_id=self._current_template_id)
        return True

    async def _fail_and_release(self, lead_id: str, outcome: str) -> bool:
        """Mark FINISHED with a terminal outcome and release the lock."""
        if not await self._lead_is_ours():
            return True
        try:
            await update_lead_call_completion_details(
                id=lead_id,
                status=LeadCallStatus.FINISHED,
                outcome=outcome,
                meta_data={"reason": f"Dispatcher: {outcome}"},
                call_end_time=datetime.now(timezone.utc),
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"fail_and_release update_completion failed: {e}")
        return await self._release(lead_id)


# ---------------------------------------------------------------------------
# Worker pool management
# ---------------------------------------------------------------------------

_workers: List[Worker] = []


async def start_workers(count: Optional[int] = None) -> List[Worker]:
    """Start ``count`` workers (default: BB_WORKER_COUNT). Idempotent."""
    global _workers
    if _workers:
        return _workers
    n = BB_WORKER_COUNT if count is None else count
    _workers = [Worker() for _ in range(n)]
    for w in _workers:
        await w.start()
    logger.info(f"Started {n} dispatch workers")
    return _workers


async def stop_workers() -> None:
    """Stop all workers: idle ones at once, committed dials drained (bounded)."""
    global _workers
    if not _workers:
        return
    logger.info(f"Stopping {len(_workers)} dispatch workers")
    await asyncio.gather(*(w.stop() for w in _workers), return_exceptions=True)
    _workers = []

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
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, cast

import aiohttp

from app.ai.voice.agents.breeze_buddy.dispatch.alerts import (
    raise_no_telephony_number,
)
from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    acquire_channel_token,
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
    schedule_lead,
)
from app.ai.voice.agents.breeze_buddy.managers.calls import (
    _acquire_number,
    _get_available_number,
    _get_lead_config,
    _is_within_calling_hours,
    _release_number,
    _run_pre_checks_for_lead,
)
from app.ai.voice.agents.breeze_buddy.managers.utils import (
    prepare_and_store_initial_greeting,
)
from app.ai.voice.agents.breeze_buddy.services.call_limiter import (
    peek_outbound_rate_limit_and_alert,
    record_outbound_call_attempt,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.utils import get_voice_provider
from app.ai.voice.agents.breeze_buddy.utils.playground import (
    apply_playground_overrides,
)
from app.core.config import dynamic as dyn_cfg
from app.core.config.static import (
    BB_CHANNEL_WAIT_BACKOFF_MAX_S,
    BB_WORKER_BLPOP_TIMEOUT_S,
    BB_WORKER_COUNT,
    BB_WORKER_HEARTBEAT_REFRESH_S,
    BB_WORKER_HEARTBEAT_TTL_S,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor import (
    acquire_lock_on_lead_by_id,
    defer_lead_next_attempt_and_release_lock,
    get_lead_by_id,
    get_template_by_id,
    is_number_blacklisted,
    release_lock_on_lead_by_id,
    update_lead_call_completion_details,
    update_lead_call_details,
)
from app.schemas import ExecutionMode, LeadCallStatus
from app.services.redis import get_redis_service

# ---------------------------------------------------------------------------
# Single dispatch worker
# ---------------------------------------------------------------------------


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

    @property
    def uuid(self) -> str:
        return self._uuid

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"bb-worker-hb-{self._uuid}"
        )
        self._task = asyncio.create_task(self._loop(), name=f"bb-worker-{self._uuid}")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(
                    self._task, timeout=BB_WORKER_BLPOP_TIMEOUT_S + 5
                )
            except asyncio.TimeoutError:
                self._task.cancel()
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
        while not self._stopping.is_set():
            try:
                redis = await get_redis_service()
                await redis.setex(key, "1", ttl_seconds=BB_WORKER_HEARTBEAT_TTL_S)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Worker {self._uuid} heartbeat write failed: {e}")
            try:
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

        # Track in-flight for crash recovery.
        await self._rpush_processing(lead_id)
        try:
            await self._dispatch(lead_id, session)
        finally:
            await self._lrem_processing(lead_id)

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
        self, lead_id: str, session: Optional[aiohttp.ClientSession]
    ) -> None:
        """
        Run the full dispatch flow for one lead. All exit paths leave the
        lead row in a consistent state (lock released, status correct).
        """
        logger.info(f"Worker {self._uuid}: picked lead {lead_id}")

        lead = await get_lead_by_id(lead_id)
        if not lead:
            logger.warning(f"Worker {self._uuid}: lead {lead_id} not found in DB")
            return
        if lead.status != LeadCallStatus.BACKLOG:
            logger.info(
                f"Worker {self._uuid}: lead {lead_id} status is "
                f"{lead.status.value}, skipping"
            )
            return
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
            return
        if await self._reseller_paused(lead.reseller_id):
            logger.info(
                f"Worker {self._uuid}: reseller {lead.reseller_id} paused, "
                f"re-scheduling lead {lead_id}"
            )
            # Defer 30s; operator unpauses by removing the key.
            await schedule_lead(
                lead_id, datetime.now(timezone.utc) + timedelta(seconds=30)
            )
            return

        locked = await acquire_lock_on_lead_by_id(
            lead_id, expected_status=LeadCallStatus.BACKLOG
        )
        if not locked:
            logger.info(
                f"Worker {self._uuid}: lead {lead_id} could not be locked "
                "(another worker or status changed). Dropping."
            )
            return

        lock_released = False
        try:
            config = await _get_lead_config(locked)
            if not config:
                lock_released = await self._fail_and_release(locked.id, "NO_CONFIG")
                return

            if not config.enable_calling:
                logger.info(f"Worker {self._uuid}: calling disabled for lead {lead_id}")
                lock_released = await self._release(locked.id)
                return

            customer_phone = (locked.payload or {}).get("customer_mobile_number")
            if customer_phone and await is_number_blacklisted(
                customer_phone, locked.reseller_id
            ):
                await update_lead_call_completion_details(
                    id=locked.id,
                    status=LeadCallStatus.FINISHED,
                    outcome="BLACKLISTED",
                    meta_data={"reason": "Phone number is blacklisted"},
                    call_end_time=datetime.now(timezone.utc),
                )
                lock_released = await self._release(locked.id)
                return

            if not _is_within_calling_hours(config):
                # Defer until window opens — we approximate by deferring 5 min
                # and letting the reconciler/promoter re-pick. Cheaper than
                # computing the exact next window here.
                lock_released = await self._defer_and_release(locked.id, 300)
                return

            # id-only resolution: leads always carry the template_id they
            # resolved to at push time; name fallback was removed.
            template = (
                await get_template_by_id(locked.template_id)
                if locked.template_id
                else None
            )
            if not template:
                logger.error(
                    f"Worker {self._uuid}: no template found for lead "
                    f"{locked.id} (template_id={locked.template_id}, "
                    f"reseller={locked.reseller_id}). Proceeding to dial "
                    "without a template."
                )

            if not await _run_pre_checks_for_lead(config, locked, template, session):
                # _run_pre_checks_for_lead already set status to FINISHED on
                # failure; we just need to release the lock.
                lock_released = await self._release(locked.id)
                return

            if template:
                template = apply_playground_overrides(locked, template)
                await prepare_and_store_initial_greeting(
                    lead_id=locked.id,
                    payload=locked.payload or {},
                    template=template,
                )

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
                    return

            number = await _get_available_number(config, template)
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
                return

            # Channel token gate (Redis). Held until call-end webhook releases.
            token = await acquire_channel_token(number.id)
            if token is None:
                # No capacity right now — re-schedule with small jitter.
                lock_released = await self._defer_and_release(
                    locked.id, random.randint(1, BB_CHANNEL_WAIT_BACKOFF_MAX_S)
                )
                return

            # DB-side bookkeeping: ``telephony_number.status`` (Twilio) or
            # ``channels`` (Exotel/Plivo) is the operator-visible view used by
            # admin UI, analytics, and the channel-token reconciler. The Redis
            # token is the actual capacity gate; the DB write is eventually
            # consistent state.
            acquired_db = await _acquire_number(number)
            if not acquired_db:
                logger.warning(
                    f"Worker {self._uuid}: DB capacity denied for number "
                    f"{number.id} despite Redis token. Releasing token, deferring."
                )
                await release_channel_token(number.id, token)
                lock_released = await self._defer_and_release(locked.id, 5)
                return

            call_provider = get_voice_provider(
                number.provider, session, config.telephony_config
            )

            customer_mobile = (locked.payload or {}).get("customer_mobile_number")
            if not customer_mobile or not isinstance(customer_mobile, str):
                logger.error(
                    f"Worker {self._uuid}: invalid customer_mobile_number "
                    f"for lead {locked.id}"
                )
                await release_channel_token(number.id, token)
                await _release_number(number.id, number.provider)
                lock_released = await self._fail_and_release(locked.id, "INVALID_PHONE")
                return

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
                    await release_channel_token(number.id, token)
                    await _release_number(number.id, number.provider)
                    lock_released = await self._defer_and_release(locked.id, rl_defer)
                    return

            try:
                call = await call_provider.make_call_async(
                    customer_mobile,
                    number.number,
                    reseller_id=locked.reseller_id,
                    # answer-url observability tag only (never parsed back);
                    # id-only convention — no template names in routing.
                    template_name=locked.template_id or "",
                )
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"Worker {self._uuid}: provider.make_call failed for "
                    f"lead {locked.id}: {e}"
                )
                await release_channel_token(number.id, token)
                await _release_number(number.id, number.provider)
                # Backoff retry. Use defer_seconds derived from attempt_count.
                backoff = min(60, 5 * (locked.attempt_count + 1))
                lock_released = await self._defer_and_release(locked.id, backoff)
                return

            if not call or not call.get("sid"):
                logger.error(
                    f"Worker {self._uuid}: provider.make_call returned no SID "
                    f"for lead {locked.id}: {call}"
                )
                await release_channel_token(number.id, token)
                await _release_number(number.id, number.provider)
                lock_released = await self._defer_and_release(locked.id, 10)
                return

            call_sid = str(call.get("sid"))
            updated = await update_lead_call_details(
                locked.id,
                LeadCallStatus.PROCESSING,
                call_sid,
                datetime.now(timezone.utc),
                number.id,
            )
            if not updated:
                # CAS lost (status changed under us). Call is placed; we now
                # have an orphan call_sid. Log loudly and release resources.
                logger.error(
                    f"Worker {self._uuid}: post-make_call CAS lost for lead "
                    f"{locked.id} (call_sid={call_sid}). Releasing resources; "
                    "the call may be orphaned."
                )
                await release_channel_token(number.id, token)
                await _release_number(number.id, number.provider)
                lock_released = await self._release(locked.id)
                return

            # Success — token stays held until call-end webhook fires.
            # Lock stays held too; the call-end webhook releases it. Setting
            # lock_released so the defensive finally below doesn't unlock the
            # row out from under the webhook handler.
            lock_released = True
            logger.info(
                f"Worker {self._uuid}: dialled lead {locked.id} via "
                f"{number.provider.value} number {number.id} (call_sid={call_sid})"
            )
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

    async def _release(self, lead_id: str) -> bool:
        try:
            await release_lock_on_lead_by_id(lead_id)
        except Exception as e:  # noqa: BLE001
            logger.error(f"release_lock failed for {lead_id}: {e}")
        return True

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
        await schedule_lead(lead_id, next_at)
        return True

    async def _fail_and_release(self, lead_id: str, outcome: str) -> bool:
        """Mark FINISHED with a terminal outcome and release the lock."""
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
    """Stop all workers. Graceful drain up to BLPOP timeout per worker."""
    global _workers
    if not _workers:
        return
    logger.info(f"Stopping {len(_workers)} dispatch workers")
    await asyncio.gather(*(w.stop() for w in _workers), return_exceptions=True)
    _workers = []

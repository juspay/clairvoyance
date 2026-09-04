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
from enum import Enum
from typing import Any, Dict, List, Optional, cast

import aiohttp

from app.ai.voice.agents.breeze_buddy.crm_mirror import (
    is_non_customer_lead,
    mirror_to_crm,
)
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
from app.ai.voice.agents.breeze_buddy.managers.pre_checks import PreCheckDecision
from app.ai.voice.agents.breeze_buddy.managers.utils import (
    prepare_and_store_initial_greeting,
)
from app.ai.voice.agents.breeze_buddy.services.call_limiter import (
    peek_outbound_rate_limit_and_alert,
    record_outbound_call_attempt,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    OutboundCallContext,
    OutboundCallPlacement,
    OutboundCallPlacementKind,
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
    append_metadata_field,
    defer_lead_next_attempt_and_release_lock,
    finish_lead_call_and_release_lock,
    get_lead_by_id,
    get_template_by_id,
    is_number_blacklisted,
    mark_provider_submission_processing,
    release_lock_on_lead_by_id,
    update_lead_call_details,
)
from app.schemas import CallProvider, ExecutionMode, LeadCallStatus
from app.services.redis import get_redis_service
from app.utils.phone_number import normalize_phone_number

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


# ---------------------------------------------------------------------------
# Single dispatch worker
# ---------------------------------------------------------------------------


class DispatchExit(str, Enum):
    """Final lock action for one dispatch iteration."""

    NEEDS_UNLOCK = "needs_unlock"
    CLEANED_UP = "cleaned_up"
    HOLD_LOCK = "hold_lock"


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
        except Exception as e:  # noqa: BLE001
            logger.warning(f"BB_DISPATCH_ENABLED lookup failed; failing open: {e}")
            return False

    async def _reseller_paused(self, reseller_id: str) -> bool:
        try:
            redis = await get_redis_service()
            return await redis.exists(reseller_paused_key(reseller_id))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"reseller pause lookup failed for reseller {reseller_id}: {e}"
            )
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
        lead row in an explicit exit state: released/deferred, terminal, or
        intentionally held for the call-end/callback lifecycle.
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

        exit_state = DispatchExit.NEEDS_UNLOCK
        try:
            config = await _get_lead_config(locked)
            if not config:
                exit_state = await self._fail_and_release(locked.id, "NO_CONFIG")
                return

            if not config.enable_calling:
                logger.info(f"Worker {self._uuid}: calling disabled for lead {lead_id}")
                exit_state = await self._release(locked.id)
                return

            phone_result = normalize_phone_number(
                (locked.payload or {}).get("customer_mobile_number")
            )
            if not phone_result.is_dialable:
                logger.warning(
                    f"Worker {self._uuid}: terminal INVALID_PHONE for lead "
                    f"{locked.id}: {phone_result.reason}"
                )
                exit_state = await self._fail_and_release(
                    locked.id, "INVALID_PHONE", reason=phone_result.reason
                )
                return
            customer_phone = str(phone_result.e164)

            # Make legacy-but-valid formatted values canonical for all work in
            # this dispatch. The API persists canonical E.164 for new rows.
            if locked.payload is not None:
                locked.payload["customer_mobile_number"] = customer_phone

            if await is_number_blacklisted(customer_phone, locked.reseller_id):
                exit_state = await self._fail_and_release(
                    locked.id,
                    "BLACKLISTED",
                    reason="Phone number is blacklisted",
                )
                return

            if not _is_within_calling_hours(config):
                # Defer until window opens — we approximate by deferring 5 min
                # and letting the reconciler/promoter re-pick. Cheaper than
                # computing the exact next window here.
                exit_state = await self._defer_and_release(locked.id, 300)
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

            pre_check_decision, pre_check_defer = await _run_pre_checks_for_lead(
                config, locked, template, session
            )
            if pre_check_decision is PreCheckDecision.ABORT:
                # _run_pre_checks_for_lead already set status to FINISHED on
                # failure; we just need to release the lock.
                exit_state = await self._release(locked.id)
                return
            if pre_check_decision is PreCheckDecision.DEFER:
                # Transient block (cooldown, quota). Lead stays BACKLOG and
                # comes back after pre_check_defer seconds.
                exit_state = await self._defer_and_release(locked.id, pre_check_defer)
                return

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
                    customer_phone=customer_phone,
                    lead_id=str(locked.id),
                    reseller_id=locked.reseller_id,
                )
                if not rate_ok:
                    exit_state = await self._defer_and_release(locked.id, defer_seconds)
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
                exit_state = await self._fail_and_release(
                    locked.id, "NUMBER_UNAVAILABLE"
                )
                return

            # Channel token gate (Redis). Held until call-end webhook releases.
            token = await acquire_channel_token(number.id)
            if token is None:
                # No capacity right now — re-schedule with small jitter.
                exit_state = await self._defer_and_release(
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
                exit_state = await self._defer_and_release(locked.id, 5)
                return

            call_provider = get_voice_provider(
                number.provider, session, config.telephony_config
            )

            customer_mobile = customer_phone

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
                    customer_phone=customer_phone,
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
                    exit_state = await self._defer_and_release(locked.id, rl_defer)
                    return

            # Greeting pre-warm as late as possible — after every gate,
            # immediately before the dial — so TTS/generation spend is
            # proportional to actual dials, not dispatch attempts. The
            # channel token acquired above is held during the bounded
            # wait (by design: dials pacing to generation capacity under
            # degradation). Bounded and fail-open — a slow or hung
            # generator never blocks the dial; the answer-time path
            # retries synthesis as a cache miss.
            if template:
                try:
                    await _prewarm_initial_greeting_with_retry(
                        lead_id=locked.id,
                        payload=locked.payload or {},
                        template=template,
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
                    await release_channel_token(number.id, token)
                    await _release_number(number.id, number.provider)
                    raise

            try:
                call = await call_provider.make_call_async(
                    customer_mobile,
                    number.number,
                    reseller_id=locked.reseller_id,
                    # Callback identity context for providers whose final call
                    # id is delivered asynchronously by webhook.
                    template_name=locked.template_id or "",
                    context=OutboundCallContext(
                        reseller_id=locked.reseller_id,
                        template_id=locked.template_id,
                        lead_id=str(locked.id),
                        telephony_number_id=str(number.id),
                    ),
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
                exit_state = await self._defer_and_release(locked.id, backoff)
                return

            if call and call.kind == OutboundCallPlacementKind.SUBMITTED:
                exit_state = await self._hold_for_provider_submission(
                    locked.id, number.id, number.provider, token, call
                )
                return

            if call and call.kind == OutboundCallPlacementKind.UNKNOWN:
                await release_channel_token(number.id, token)
                await _release_number(number.id, number.provider)
                metadata_updated = await append_metadata_field(
                    locked.id,
                    {
                        "provider_call_submission": {
                            "provider": number.provider.value,
                            "status": "unknown_after_timeout",
                            "error_type": call.error_type,
                            "message": call.message,
                        }
                    },
                )
                if metadata_updated is None:
                    logger.error(
                        f"Worker {self._uuid}: failed to persist ambiguous "
                        f"Plivo submission metadata for lead {locked.id}"
                    )
                exit_state = await self._fail_and_release(
                    locked.id,
                    "PLACEMENT_UNKNOWN",
                    reason=(
                        call.message
                        or "Provider submission timed out; outcome unknown and not retried"
                    ),
                )
                return

            if call and call.kind == OutboundCallPlacementKind.REJECTED:
                await release_channel_token(number.id, token)
                await _release_number(number.id, number.provider)
                if call.retryable:
                    backoff = min(60, 5 * (locked.attempt_count + 1))
                    logger.warning(
                        f"Worker {self._uuid}: retryable provider call submission "
                        f"failure for lead {locked.id} "
                        f"(error_type={call.error_type}). Deferring {backoff}s."
                    )
                    exit_state = await self._defer_and_release(locked.id, backoff)
                    return

                logger.error(
                    f"Worker {self._uuid}: provider call submission rejected "
                    f"for lead {locked.id} (error_type={call.error_type})"
                )
                exit_state = await self._fail_and_release(
                    locked.id,
                    "PLACEMENT_REJECTED",
                    reason=str(call.message or call.error_type or call),
                )
                return

            if (
                not call
                or call.kind != OutboundCallPlacementKind.STARTED
                or not call.sid
            ):
                logger.error(
                    f"Worker {self._uuid}: provider call placement returned no started call id "
                    f"for lead {locked.id}: {call}"
                )
                await release_channel_token(number.id, token)
                await _release_number(number.id, number.provider)
                exit_state = await self._defer_and_release(locked.id, 10)
                return

            call_sid = call.sid

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
                exit_state = await self._release(locked.id)
                return

            # Success — token stays held until call-end webhook fires.
            # Lock stays held too; the call-end webhook releases it. Setting
            # HOLD_LOCK so the defensive finally below doesn't unlock the row
            # out from under the webhook handler.
            exit_state = DispatchExit.HOLD_LOCK
            logger.info(
                f"Worker {self._uuid}: dialled lead {locked.id} via "
                f"{number.provider.value} number {number.id} (call_sid={call_sid})"
            )
        finally:
            if exit_state == DispatchExit.NEEDS_UNLOCK:
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

    async def _release(self, lead_id: str) -> DispatchExit:
        try:
            await release_lock_on_lead_by_id(lead_id)
        except Exception as e:  # noqa: BLE001
            logger.error(f"release_lock failed for {lead_id}: {e}")
        return DispatchExit.CLEANED_UP

    async def _defer_and_release(
        self, lead_id: str, defer_seconds: int
    ) -> DispatchExit:
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
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"release_lock failed after defer failure for {lead_id}: {e}"
                )
            return DispatchExit.CLEANED_UP

        # DB defer succeeded — mirror the DB-authoritative timestamp in Redis.
        # Fall back to now+defer_seconds only if the DB column was somehow
        # NULL (shouldn't happen post-defer, but defensive).
        next_at = deferred.next_attempt_at or (
            datetime.now(timezone.utc) + timedelta(seconds=defer_seconds)
        )
        await schedule_lead(lead_id, next_at)
        return DispatchExit.CLEANED_UP

    async def _fail_and_release(
        self, lead_id: str, outcome: str, *, reason: str | None = None
    ) -> DispatchExit:
        """Mark FINISHED with a terminal outcome and release the lock atomically."""
        finished = await finish_lead_call_and_release_lock(
            id=lead_id,
            outcome=outcome,
            meta_data={"reason": reason or f"Dispatcher: {outcome}"},
            call_end_time=datetime.now(timezone.utc),
        )
        if finished is None:
            logger.error(
                f"fail_and_release terminal update failed for {lead_id} "
                f"(outcome={outcome}); keeping lock held for investigation"
            )
            return DispatchExit.HOLD_LOCK
        return DispatchExit.CLEANED_UP

    async def _hold_for_provider_submission(
        self,
        lead_id: str,
        telephony_number_id: str,
        provider: CallProvider,
        token: str,
        placement: OutboundCallPlacement,
    ) -> DispatchExit:
        """Persist an accepted provider submission and keep resources held."""
        if not placement.submission_id:
            logger.error(
                f"provider submitted call for lead {lead_id} without a submission id"
            )
            await release_channel_token(telephony_number_id, token)
            await _release_number(telephony_number_id, provider)
            return await self._fail_and_release(
                lead_id,
                "PLACEMENT_REJECTED",
                reason="Provider submission missing request id",
            )

        submission_metadata = {
            "provider_call_submission": {
                "provider": provider.value,
                "telephony_number_id": telephony_number_id,
                "request_uuid": placement.submission_id,
                "api_id": placement.api_id,
                "status": "accepted_waiting_for_call_uuid",
            }
        }
        updated = await mark_provider_submission_processing(
            lead_id,
            placement.submission_id,
            datetime.now(timezone.utc),
            telephony_number_id,
            submission_metadata,
        )
        if updated is None:
            logger.error(
                f"failed to mark submitted provider call PROCESSING for lead {lead_id}; "
                "releasing local resources because callback reconciliation is not safe"
            )
            await release_channel_token(telephony_number_id, token)
            await _release_number(telephony_number_id, provider)
            return await self._defer_and_release(lead_id, 60)

        return DispatchExit.HOLD_LOCK


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

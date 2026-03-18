"""Redis-backed circuit breaker for STT provider fallback.

Tracks Soniox failures across pods via Redis. When failures exceed a
configurable threshold within a time window, the circuit trips OPEN and
all new calls use Deepgram directly — no Soniox init, no idle WebSocket.

After a cooldown (OPEN_DURATION), the circuit enters HALF-OPEN: a single
probe call (protected by ServiceSwitcher) tests Soniox. If the probe
succeeds, the circuit closes. If it fails, it re-trips.

State machine::

                  failure_count >= FAILURE_THRESHOLD
   ┌─────────┐  ─────────────────────────────────►  ┌──────────┐
   │ CLOSED  │                                       │   OPEN   │
   │(Soniox) │                                       │(Deepgram)│
   └─────────┘                                       └──────────┘
       ▲                                                  │
       │ success        TTL expires (30 min)              │
       │            ┌────────────┐                        │
       └────────────│ HALF-OPEN  │◄───────────────────────┘
                    │(probe call)│  failure >= RETRIP_THRESHOLD
                    └────────────┘──── → re-trip to OPEN

Redis keys (all under ``stt:cb:`` prefix):
    - ``failure_count``  - atomic counter, TTL = FAILURE_WINDOW_SECS
    - ``open``           - presence flag, TTL = OPEN_DURATION_SECS
    - ``half_open``      - recovery signal, TTL = OPEN_DURATION + PROBE_LOCK_TTL + 60
    - ``probe_lock``     - NX lock, TTL = PROBE_LOCK_TTL_SECS
"""

from __future__ import annotations

import asyncio
import enum

from app.core.config.static import (
    ENABLE_BREEZE_BUDDY_STT_FALLBACK,
    STT_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    STT_CIRCUIT_BREAKER_FAILURE_WINDOW_SECS,
    STT_CIRCUIT_BREAKER_OPEN_DURATION_SECS,
    STT_CIRCUIT_BREAKER_PROBE_LOCK_TTL_SECS,
    STT_CIRCUIT_BREAKER_RETRIP_THRESHOLD,
)
from app.core.logger import logger
from app.services.redis import get_redis_service
from app.services.slack import slack_alert

# ---------------------------------------------------------------------------
# Redis key constants
# ---------------------------------------------------------------------------
_KEY_FAILURE_COUNT = "stt:cb:failure_count"
_KEY_OPEN = "stt:cb:open"
_KEY_HALF_OPEN = "stt:cb:half_open"
_KEY_PROBE_LOCK = "stt:cb:probe_lock"

# Background task references to prevent premature GC of fire-and-forget tasks
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    """Schedule a coroutine as a fire-and-forget task, preventing GC."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


class CircuitState(enum.Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class STTCircuitBreaker:
    """Redis-backed circuit breaker for Soniox → Deepgram STT fallback.

    All state lives in Redis so the breaker is shared across pods.
    Methods are safe to call concurrently — Redis operations are atomic.

    Usage::

        from app.ai.voice.agents.breeze_buddy.stt.fallback import stt_circuit_breaker

        state = await stt_circuit_breaker.get_state()
        if state == CircuitState.OPEN:
            # Use Deepgram directly
            ...
    """

    # -----------------------------------------------------------------------
    # State queries
    # -----------------------------------------------------------------------

    async def get_state(self) -> CircuitState:
        """Return the current circuit state by inspecting Redis keys.

        - ``stt:cb:open`` exists      → OPEN
        - ``stt:cb:half_open`` exists  → HALF_OPEN  (``open`` has expired)
        - neither                      → CLOSED
        """
        if not ENABLE_BREEZE_BUDDY_STT_FALLBACK:
            return CircuitState.CLOSED

        try:
            redis = await get_redis_service()

            if await redis.exists(_KEY_OPEN):
                return CircuitState.OPEN

            # ``stt:cb:open`` expired — check the dedicated half-open flag
            # set during ``_trip()``.  Its longer TTL guarantees a probe
            # window after the open period ends.
            if await redis.exists(_KEY_HALF_OPEN):
                return CircuitState.HALF_OPEN

            return CircuitState.CLOSED
        except Exception as exc:
            logger.warning(
                f"Circuit breaker state check failed (defaulting to CLOSED): {exc}"
            )
            return CircuitState.CLOSED

    async def is_open(self) -> bool:
        """Convenience: ``True`` when circuit is OPEN."""
        return (await self.get_state()) == CircuitState.OPEN

    # -----------------------------------------------------------------------
    # Failure recording
    # -----------------------------------------------------------------------

    async def record_failure(self) -> None:
        """Record an STT failure and trip the circuit if threshold is reached.

        In HALF-OPEN state, uses ``RETRIP_THRESHOLD`` (typically 1) so a
        single probe failure immediately re-trips the circuit.
        """
        if not ENABLE_BREEZE_BUDDY_STT_FALLBACK:
            return

        try:
            redis = await get_redis_service()
            state = await self.get_state()

            # Increment failure counter
            count = await redis.incr(_KEY_FAILURE_COUNT)

            # Set TTL on the counter only on the first failure (count == 1)
            # so the window starts fresh. Subsequent failures within the
            # window just increment without resetting the TTL.
            if count == 1:
                await redis.expire(
                    _KEY_FAILURE_COUNT, STT_CIRCUIT_BREAKER_FAILURE_WINDOW_SECS
                )

            # Determine threshold based on current state
            threshold = (
                STT_CIRCUIT_BREAKER_RETRIP_THRESHOLD
                if state == CircuitState.HALF_OPEN
                else STT_CIRCUIT_BREAKER_FAILURE_THRESHOLD
            )

            logger.info(
                f"STT circuit breaker: failure recorded "
                f"(count={count}, threshold={threshold}, state={state.value})"
            )

            if count >= threshold:
                await self._trip(state)
        except Exception as exc:
            # Circuit breaker is best-effort — never block the call path
            logger.warning(f"Circuit breaker record_failure failed: {exc}")

    async def _trip(self, previous_state: CircuitState) -> None:
        """Trip the circuit to OPEN state.

        Uses SET NX on the open key so that only the first caller in a
        concurrent burst actually trips the circuit and sends the alert.
        Subsequent callers see NX fail and skip — preventing duplicate alerts.
        """
        try:
            redis = await get_redis_service()

            # Atomically set open flag — only proceeds if key didn't exist
            newly_tripped = await redis.set(
                _KEY_OPEN,
                "1",
                nx=True,
                ex=STT_CIRCUIT_BREAKER_OPEN_DURATION_SECS,
            )

            if not newly_tripped:
                # Another pod/call already tripped — skip duplicate work
                logger.info(
                    "STT circuit breaker: _trip called but circuit already OPEN "
                    "(another caller tripped first). Skipping."
                )
                return

            # Set dedicated half-open flag with a longer TTL so that
            # HALF_OPEN is reachable after the open period expires.
            # TTL = open_duration + probe_lock_ttl + 60s safety buffer.
            half_open_ttl = (
                STT_CIRCUIT_BREAKER_OPEN_DURATION_SECS
                + STT_CIRCUIT_BREAKER_PROBE_LOCK_TTL_SECS
                + 60
            )
            await redis.set(
                _KEY_HALF_OPEN,
                "1",
                ex=half_open_ttl,
            )

            # Reset failure counter for clean HALF-OPEN start
            await redis.delete(_KEY_FAILURE_COUNT)

            action = (
                "re-tripped (probe failed)"
                if previous_state == CircuitState.HALF_OPEN
                else "tripped"
            )
            logger.warning(
                f"STT circuit breaker {action} → OPEN for "
                f"{STT_CIRCUIT_BREAKER_OPEN_DURATION_SECS}s. "
                f"All calls will use Deepgram."
            )

            # Non-blocking Slack alert
            cooldown_min = STT_CIRCUIT_BREAKER_OPEN_DURATION_SECS // 60

            async def _send_trip_alert():
                try:
                    await slack_alert.send(
                        title="🔴 Soniox STT Down — Switched to Deepgram (Breeze Buddy)",
                        fields=[
                            {"name": "Status", "value": action.capitalize()},
                            {
                                "name": "Cooldown",
                                "value": f"{cooldown_min} min",
                            },
                        ],
                        sections=[
                            {
                                "title": "What Happened",
                                "text": (
                                    "Soniox STT failed repeatedly. "
                                    "All Breeze Buddy calls are now using Deepgram.\n"
                                    f"Soniox will be automatically retested after {cooldown_min} minutes."
                                ),
                            }
                        ],
                        fallback_text=(
                            f"Soniox STT down — Deepgram active for {cooldown_min} min"
                        ),
                    )
                except Exception as alert_err:
                    logger.warning(
                        f"Failed to send Soniox-down Slack alert: {alert_err}"
                    )

            _fire_and_forget(_send_trip_alert())
        except Exception as exc:
            logger.warning(f"Circuit breaker _trip failed: {exc}")

    # -----------------------------------------------------------------------
    # Success recording (HALF-OPEN → CLOSED)
    # -----------------------------------------------------------------------

    async def record_success(self) -> None:
        """Record a successful Soniox call, closing the circuit if HALF-OPEN.

        Only meaningful in HALF-OPEN state. In CLOSED state this is a no-op
        (avoids unnecessary Redis writes on the happy path).
        """
        if not ENABLE_BREEZE_BUDDY_STT_FALLBACK:
            return

        try:
            state = await self.get_state()
            if state != CircuitState.HALF_OPEN:
                return

            redis = await get_redis_service()

            # Clear all circuit breaker keys → CLOSED
            await redis.delete(_KEY_FAILURE_COUNT)
            await redis.delete(_KEY_OPEN)
            await redis.delete(_KEY_HALF_OPEN)

            logger.info(
                "STT circuit breaker: probe succeeded → CLOSED. "
                "Soniox restored for all calls."
            )

            # Non-blocking Slack alert
            async def _send_recovery_alert():
                try:
                    await slack_alert.send(
                        title="🟢 Soniox STT Recovered (Breeze Buddy)",
                        fields=[
                            {"name": "Status", "value": "Recovered"},
                        ],
                        sections=[
                            {
                                "title": "What Happened",
                                "text": (
                                    "Soniox test call completed successfully. "
                                    "All Breeze Buddy calls will now use Soniox again."
                                ),
                            }
                        ],
                        fallback_text="Soniox STT recovered — all calls back to Soniox",
                    )
                except Exception as alert_err:
                    logger.warning(
                        f"Failed to send Soniox recovery Slack alert: {alert_err}"
                    )

            _fire_and_forget(_send_recovery_alert())
        except Exception as exc:
            logger.warning(f"Circuit breaker record_success failed: {exc}")

    # -----------------------------------------------------------------------
    # Probe lock management (HALF-OPEN single-caller guard)
    # -----------------------------------------------------------------------

    async def try_acquire_probe(self) -> bool:
        """Attempt to acquire the probe lock (NX).

        Returns ``True`` if this call won the lock and should probe Soniox
        with ServiceSwitcher. Returns ``False`` if another call is already
        probing — the caller should use Deepgram directly.

        The lock auto-expires after ``PROBE_LOCK_TTL_SECS`` (default 420s /
        7 min) as a safety net if the probe call crashes without releasing.
        """
        if not ENABLE_BREEZE_BUDDY_STT_FALLBACK:
            return False

        try:
            redis = await get_redis_service()
            acquired = await redis.set(
                _KEY_PROBE_LOCK,
                "1",
                nx=True,
                ex=STT_CIRCUIT_BREAKER_PROBE_LOCK_TTL_SECS,
            )

            if acquired:
                logger.info(
                    "STT circuit breaker: probe lock acquired — "
                    "this call will test Soniox with Deepgram fallback."
                )

                # Non-blocking Slack alert
                async def _send_probe_alert():
                    try:
                        await slack_alert.send(
                            title="🟡 Testing Soniox Recovery (Breeze Buddy)",
                            fields=[
                                {"name": "Status", "value": "Testing"},
                            ],
                            sections=[
                                {
                                    "title": "What Happened",
                                    "text": (
                                        "Cooldown period has ended. "
                                        "A single call is now testing whether Soniox has recovered. "
                                        "Other calls continue using Deepgram."
                                    ),
                                }
                            ],
                            fallback_text="Testing Soniox recovery — one call probing",
                        )
                    except Exception as alert_err:
                        logger.warning(
                            f"Failed to send Soniox recovery test Slack alert: {alert_err}"
                        )

                _fire_and_forget(_send_probe_alert())
            else:
                logger.info(
                    "STT circuit breaker: probe lock not acquired — "
                    "another call is already probing. Using Deepgram."
                )

            return acquired
        except Exception as exc:
            logger.warning(
                f"Circuit breaker try_acquire_probe failed "
                f"(defaulting to no probe): {exc}"
            )
            return False

    async def release_probe(self) -> None:
        """Release the probe lock.

        Called after a probe call completes (success or failure) so the
        next HALF-OPEN window can immediately start a new probe.
        """
        if not ENABLE_BREEZE_BUDDY_STT_FALLBACK:
            return

        try:
            redis = await get_redis_service()
            await redis.delete(_KEY_PROBE_LOCK)
            logger.info("STT circuit breaker: probe lock released.")
        except Exception as exc:
            logger.warning(f"Circuit breaker release_probe failed: {exc}")


# ---------------------------------------------------------------------------
# Module-level singleton (like slack_alert)
# ---------------------------------------------------------------------------
stt_circuit_breaker = STTCircuitBreaker()

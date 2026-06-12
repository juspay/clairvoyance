"""In-process approval manager for Daily voice mode (HITL Pattern C).

The gate (template/approval.py) calls ``request()`` from inside a function
handler task; the end user's decision arrives via an RTVI
``function-approval-decision`` client message routed to ``resolve()``.

Lifecycle invariants (these are load-bearing — see plan review):
- ``asyncio.wait_for`` CANCELS the inner future on timeout, so a late
  ``resolve()`` must done-guard (no InvalidStateError) and report stale.
- Every exit path (approve / deny / timeout / CancelledError / deny_all /
  supersede) pops the map entry exactly once and emits exactly one
  ``function-approval-resolved`` event, both owned by ``request()``'s
  try/finally so there is a single emission point.
- On CancelledError (user barge-in cancels sync gated handlers with ~1s
  grace; pipeline teardown) the map pop is synchronous and the emit is a
  cheap frame-queue put, so cleanup fits the grace window; the error is
  re-raised so pipecat records the call as CANCELLED.
- Duplicate request for the same function name (async re-call, parallel
  batch) supersedes the older pending request.
"""

import asyncio
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.template.approval import (
    WIRE_STATUS_APPROVED,
    WIRE_STATUS_CANCELLED,
    WIRE_STATUS_DENIED,
    WIRE_STATUS_SUPERSEDED,
    WIRE_STATUS_TIMEOUT,
    ApprovalOutcome,
)
from app.ai.voice.agents.breeze_buddy.template.types import ApprovalConfig
from app.core.logger import logger

# RTVI message types (kebab-case, matching the existing server-message
# convention — see handlers/transport/rtvi.py and docs/DAILY_RTVI_EVENTS.md).
RTVI_APPROVAL_REQUEST = "function-approval-request"
RTVI_APPROVAL_RESOLVED = "function-approval-resolved"
RTVI_APPROVAL_DECISION = "function-approval-decision"

EmitFn = Callable[..., Awaitable[None]]

# Future resolves to (approved, wire_status, reason).
_Resolution = Tuple[bool, str, Optional[str]]

# Hard ceiling on the in-process wait. The pipeline's idle watchdog fires at
# 300s of speech inactivity and tears the WHOLE call down (deny_all + BUSY);
# a wait clamped below it resolves as a timeout the LLM can talk about
# instead. ApprovalConfig only validates gt=0 (a tighter bound would make
# old templates fail validation and silently UN-gate the function), so the
# clamp lives here.
_MAX_WAIT_SECS = 270.0


class ApprovalManager:
    """Tracks pending approval requests for one in-process voice bot."""

    def __init__(self, emit: EmitFn):
        self._emit = emit
        self._pending: Dict[
            str, Tuple["asyncio.Future[_Resolution]", Dict[str, Any]]
        ] = {}
        # function_name -> approval_id, for supersede-on-duplicate.
        self._by_function: Dict[str, str] = {}

    def has_pending(self) -> bool:
        return bool(self._pending)

    def pending_requests(self) -> List[Dict[str, Any]]:
        """Request payloads for re-emit on client reconnect."""
        return [payload for (_fut, payload) in self._pending.values()]

    async def request(
        self,
        function_name: str,
        arguments: Dict[str, Any],
        config: ApprovalConfig,
    ) -> ApprovalOutcome:
        """Emit an approval request and wait for the decision."""
        # Supersede an older pending request for the same function — its
        # awaiting request() coroutine wakes up and handles its own
        # cleanup + resolved emit.
        prior_id = self._by_function.get(function_name)
        if prior_id is not None:
            prior = self._pending.get(prior_id)
            if prior is not None and not prior[0].done():
                logger.info(
                    f"[approval] Superseding pending approval {prior_id} "
                    f"for '{function_name}' with a newer request"
                )
                prior[0].set_result(
                    (
                        False,
                        WIRE_STATUS_SUPERSEDED,
                        "superseded by a newer request for the same function",
                    )
                )

        wait_secs = config.timeout_secs
        if wait_secs > _MAX_WAIT_SECS:
            logger.warning(
                f"[approval] timeout_secs={wait_secs} exceeds the pipeline "
                f"idle budget; clamping to {_MAX_WAIT_SECS}s for "
                f"'{function_name}'"
            )
            wait_secs = _MAX_WAIT_SECS

        approval_id = str(uuid.uuid4())
        payload: Dict[str, Any] = {
            "approval_id": approval_id,
            "function_name": function_name,
            "arguments": arguments or {},
            "prompt": config.prompt,
            "timeout_secs": wait_secs,
        }
        fut: "asyncio.Future[_Resolution]" = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = (fut, payload)
        self._by_function[function_name] = approval_id

        outcome: Optional[ApprovalOutcome] = None
        try:
            logger.info(
                f"[approval] Requesting approval for '{function_name}' "
                f"(approval_id={approval_id}, timeout={wait_secs}s)"
            )
            await self._emit(RTVI_APPROVAL_REQUEST, payload)
            try:
                approved, status, reason = await asyncio.wait_for(
                    fut, timeout=wait_secs
                )
                outcome = ApprovalOutcome(
                    approved=approved, status=status, reason=reason
                )
            except asyncio.TimeoutError:
                outcome = ApprovalOutcome(
                    approved=False,
                    status=WIRE_STATUS_TIMEOUT,
                    reason="approval request timed out",
                )
            return outcome
        except asyncio.CancelledError:
            # Barge-in on a sync gated function or pipeline teardown.
            outcome = ApprovalOutcome(
                approved=False,
                status=WIRE_STATUS_CANCELLED,
                reason="cancelled by user interruption or call teardown",
            )
            raise
        finally:
            # Single cleanup + emission point for every exit path.
            self._pending.pop(approval_id, None)
            if self._by_function.get(function_name) == approval_id:
                self._by_function.pop(function_name, None)
            if outcome is not None:
                logger.info(
                    f"[approval] Resolved '{function_name}' "
                    f"(approval_id={approval_id}) -> {outcome.status}"
                    + (f" ({outcome.reason})" if outcome.reason else "")
                )
                try:
                    await self._emit(
                        RTVI_APPROVAL_RESOLVED,
                        {"approval_id": approval_id, "status": outcome.status},
                    )
                except Exception as e:
                    logger.warning(
                        f"[approval] Failed to emit resolution for "
                        f"{approval_id}: {e}"
                    )

    def resolve(
        self, approval_id: str, approved: bool, reason: Optional[str] = None
    ) -> bool:
        """Apply a client decision. Returns False for stale/unknown ids.

        Idempotent: duplicate decisions and decisions arriving after a
        timeout (the future is already done/cancelled) are no-ops.
        """
        entry = self._pending.get(approval_id)
        if entry is None or entry[0].done():
            logger.warning(
                f"[approval] Ignoring decision for stale/unknown "
                f"approval_id={approval_id} (approved={approved})"
            )
            return False
        status = WIRE_STATUS_APPROVED if approved else WIRE_STATUS_DENIED
        entry[0].set_result((approved, status, reason))
        return True

    def deny_all(self, reason: str) -> None:
        """Deny every pending request (disconnect / idle / conversation end).

        Tolerates already-done futures; each awaiting ``request()`` performs
        its own cleanup and resolved-event emission.
        """
        if not self._pending:
            return
        logger.info(
            f"[approval] Denying {len(self._pending)} pending approval(s): " f"{reason}"
        )
        for _approval_id, (fut, _payload) in list(self._pending.items()):
            if not fut.done():
                fut.set_result((False, WIRE_STATUS_DENIED, reason))

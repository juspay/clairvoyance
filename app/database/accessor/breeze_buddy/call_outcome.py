"""The gate for call outcome fact writes (migration 081), and the shadow check.

Every accessor that writes an outcome asks this module before naming a fact
column. Off — the default, and the answer whenever the flag cannot be read —
means the statement is exactly today's, which is what lets the code deploy
before migration 081 runs and what rolls the fact write path back.

The shadow check (phase 1 of docs/CALL_OUTCOMES.md) runs on every terminal
write: it computes ``legacy_outcome(facts)`` from the row just written and
compares it with the ``outcome`` word the old writers put there. Every
disagreement is logged with the facts that produced it, so the soak shows,
per ending, whether the one function reproduces today's word before phase 2
lets it replace the old writers. It only reads; it never changes a row, and
a failure in it is logged and swallowed.
"""

from typing import Optional

from app.core.config.dynamic import CALL_OUTCOME_WRITES_ENABLED
from app.core.logger import logger
from app.schemas import LeadCallStatus, LeadCallTracker
from app.schemas.breeze_buddy.outcomes import CallOutcome, legacy_outcome


async def call_outcome_writes_enabled() -> bool:
    """The CALL_OUTCOME_WRITES_ENABLED flag, failing closed."""
    try:
        return bool(await CALL_OUTCOME_WRITES_ENABLED())
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"CALL_OUTCOME_WRITES_ENABLED unreadable, call outcome writes off: {e}"
        )
        return False


async def gate_call_outcome(
    call_outcome: Optional[CallOutcome],
) -> Optional[CallOutcome]:
    """``call_outcome`` while fact writes are on, else None (legacy-only write)."""
    if call_outcome is None or not await call_outcome_writes_enabled():
        return None
    return call_outcome


async def check_legacy_outcome(lead: Optional[LeadCallTracker], write: str) -> None:
    """Shadow check: does ``legacy_outcome`` reproduce this terminal row's word?

    ``write`` names the statement (insert / completion / abort); the facts in
    the log name the ending. Logged as ``component=call_outcome_shadow``:
    ``shadow=match`` at debug, ``shadow=mismatch`` and ``shadow=no_facts`` (a
    terminal row with no fact recorded — a writer that records none) at
    warning, so both can be counted per ending.
    """
    if lead is None or lead.status != LeadCallStatus.FINISHED:
        return
    try:
        if not await call_outcome_writes_enabled():
            return
        written = lead.outcome or None
        derived = legacy_outcome(lead)
        log = logger.bind(
            component="call_outcome_shadow",
            lead_id=str(lead.id),
            write=write,
            legacy=written,
            derived=derived,
            connection_status=lead.connection_status,
            connection_reason=lead.connection_reason,
            end_reason=lead.end_reason,
            agent_outcome=lead.agent_outcome,
            outcome_source=lead.outcome_source,
        )
        if lead.connection_status is None and lead.agent_outcome is None:
            log.bind(shadow="no_facts").warning(
                f"call outcome shadow: lead {lead.id} finished with no facts "
                f"(outcome {written!r})"
            )
        elif derived != written:
            log.bind(shadow="mismatch").warning(
                f"call outcome shadow: lead {lead.id} outcome {written!r}, "
                f"facts give {derived!r}"
            )
        else:
            log.bind(shadow="match").debug(
                f"call outcome shadow: lead {lead.id} outcome {written!r} matches"
            )
    except Exception:  # noqa: BLE001 — the check never touches the write
        logger.opt(exception=True).warning(
            f"call outcome shadow check failed for lead {lead.id}"
        )

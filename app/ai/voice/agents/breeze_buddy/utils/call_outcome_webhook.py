"""Call outcome keys on merchant webhooks (docs/CALL_OUTCOMES.md, Phase 2 2f).

Every merchant webhook builder adds the same keys through this module, behind
the WEBHOOK_CALL_OUTCOME_KEYS switch (off by default, fails closed). The keys
are additive and always present once on — null when unknown — so a merchant
sees one stable shape. They never replace a key the payload already has.
"""

from typing import Any, Dict, Optional

from app.core.config.dynamic import WEBHOOK_CALL_OUTCOME_KEYS
from app.core.logger import logger
from app.schemas.breeze_buddy.outcomes import CallOutcome

EVENT_CALL_COMPLETED = "call.completed"
EVENT_CALL_OUTCOME_EVALUATED = "call.outcome_evaluated"


async def call_outcome_webhook_keys_enabled() -> bool:
    """The WEBHOOK_CALL_OUTCOME_KEYS flag, failing closed."""
    try:
        return bool(await WEBHOOK_CALL_OUTCOME_KEYS())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"WEBHOOK_CALL_OUTCOME_KEYS unreadable, keys off: {e}")
        return False


def call_outcome_webhook_keys(
    call_outcome: Optional[CallOutcome],
    *,
    event: str,
    eval_outcome: Optional[str] = None,
    eval_status: Optional[str] = None,
) -> Dict[str, Any]:
    """The call outcome keys, camelCase like the rest of the payload."""
    columns = call_outcome.columns() if call_outcome is not None else {}
    return {
        "event": event,
        "connectionStatus": columns.get("connection_status"),
        "connectionReason": columns.get("connection_reason"),
        "endReason": columns.get("end_reason"),
        "agentOutcome": columns.get("agent_outcome"),
        "outcomeSource": columns.get("outcome_source"),
        "evalOutcome": {"status": eval_status, "value": eval_outcome},
    }


async def with_call_outcome_keys(
    data: Dict[str, Any],
    call_outcome: Optional[CallOutcome],
    *,
    event: str = EVENT_CALL_COMPLETED,
    eval_outcome: Optional[str] = None,
    eval_status: Optional[str] = None,
) -> Dict[str, Any]:
    """``data`` plus the call outcome keys while the switch is on; ``data``
    unchanged otherwise. A key ``data`` already has always keeps its value."""
    if not await call_outcome_webhook_keys_enabled():
        return data
    keys = call_outcome_webhook_keys(
        call_outcome, event=event, eval_outcome=eval_outcome, eval_status=eval_status
    )
    return {**keys, **data}

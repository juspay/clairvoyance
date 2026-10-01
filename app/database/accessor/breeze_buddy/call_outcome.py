"""The single gate for call outcome writes (migration 080).

Every accessor that writes an outcome asks this module before naming a call outcome
column. Off — the default, and the answer whenever the flag cannot be read —
means the statement is exactly today's, which is what lets the code deploy
before migration 080 runs and what rolls the call outcome write path back.
"""

from typing import Optional

from app.core.config.dynamic import CALL_OUTCOME_WRITES_ENABLED
from app.core.logger import logger
from app.schemas.breeze_buddy.outcomes import CallOutcome


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
    """``call_outcome`` while call outcome writes are on, else None (legacy-only write)."""
    if call_outcome is None or not await call_outcome_writes_enabled():
        return None
    return call_outcome

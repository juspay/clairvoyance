"""The call-queue hooks: how the CRM asks the dialler for a call, and takes
the ask back, without importing it.

`app/crm` may not import `app.ai` (scripts/check_crm_boundaries.py rule 4),
so the dialler registers its three functions here at startup — the pattern
of the created-lead hook in accessor/breeze_buddy/lead_call_tracker.py.
This file imports neither side.

Fail-open: with no hook, or a hook that raises, the caller is never broken.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional, Tuple

from app.core.logger import logger


@dataclass(frozen=True)
class CallRequest:
    lead_id: str
    template_id: str
    run_id: str
    ready_at: datetime
    rank: int
    order: str  # "first_ready" | "newest_event"
    event_at: Optional[datetime]
    # What a live call falls to if it is not called by closing; None = no change.
    next_rank: Optional[int] = None
    next_order: Optional[str] = None


QueueFn = Callable[[CallRequest], Awaitable[Optional[bool]]]
WithdrawFn = Callable[[str, str], Awaitable[None]]
# (template_id, lead_id, rank, order, event_at), plus next_rank / next_order as
# keywords when the call has them.
RerankFn = Callable[..., Awaitable[None]]

_hooks: Optional[Tuple[QueueFn, WithdrawFn, RerankFn]] = None


def register(queue: QueueFn, withdraw: WithdrawFn, rerank: RerankFn) -> None:
    global _hooks
    _hooks = (queue, withdraw, rerank)


async def _call(index: int, failed: Any, *args: Any, **kwargs: Any) -> Any:
    if _hooks is None:
        return None
    try:
        return await _hooks[index](*args, **kwargs)
    except Exception as e:
        logger.error(f"call queue hook failed: {type(e).__name__}: {e}")
        return failed


async def queue_call(request: CallRequest) -> Optional[bool]:
    """True = queued. False = failed (the rebuilder heals). None = no line
    queue takes this call: the caller inserts the lead as it does today."""
    return await _call(0, False, request)


async def withdraw_call(template_id: str, lead_id: str) -> None:
    await _call(1, None, template_id, lead_id)


async def rerank_call(
    template_id: str,
    lead_id: str,
    rank: int,
    order: str,
    event_at: Optional[datetime],
    *,
    next_rank: Optional[int] = None,
    next_order: Optional[str] = None,
) -> None:
    """next_rank / next_order: what a live call falls to if it is not called by
    closing. They reach the hook as keywords, and only when the call has them."""
    later = (
        {} if next_rank is None else {"next_rank": next_rank, "next_order": next_order}
    )
    await _call(2, None, template_id, lead_id, rank, order, event_at, **later)

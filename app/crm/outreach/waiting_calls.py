"""Waiting-call hooks: what the CRM says about a call that waits for its line.

The CRM states its own facts — a call waits, a call left, a call's rank changed
— and whoever keeps the line (Buddy's dialler) registers to hear them through
the contract, the way crm_mirror registers on the lead table's hooks. ONE
DIRECTION: Buddy imports the CRM's contract; the CRM never imports Buddy.

Fail-open: with no hook, or a hook that raises, the caller is never broken and
a call no one took is made today's way.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional, Tuple

from app.core.logger import logger


@dataclass(frozen=True)
class WaitingCall:
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


WaitsFn = Callable[[WaitingCall], Awaitable[Optional[bool]]]
LeftFn = Callable[[str, str], Awaitable[None]]
# (template_id, lead_id, rank, order, event_at), plus next_rank / next_order as
# keywords when the call has them.
RerankedFn = Callable[..., Awaitable[None]]

_hooks: Optional[Tuple[WaitsFn, LeftFn, RerankedFn]] = None


def register(waits: WaitsFn, left: LeftFn, reranked: RerankedFn) -> None:
    global _hooks
    _hooks = (waits, left, reranked)


async def _call(index: int, *args: Any, **kwargs: Any) -> Any:
    if _hooks is None:
        return None
    try:
        return await _hooks[index](*args, **kwargs)
    except Exception as e:
        logger.error(f"waiting-call hook failed: {type(e).__name__}: {e}")
        return None


async def call_waits(call: WaitingCall) -> bool:
    """True only when a line keeper took the call; otherwise (no hook, its
    number keeps no line, or a failure) the caller makes the lead today's way."""
    return await _call(0, call) is True


async def call_left(template_id: str, lead_id: str) -> None:
    await _call(1, template_id, lead_id)


async def call_reranked(
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
    await _call(2, template_id, lead_id, rank, order, event_at, **later)

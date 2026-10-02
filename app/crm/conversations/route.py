"""What a customer's message on Buddy's number does to its thread (R2) —
PURE: the projector gathers the thread and its open handoff, asks here, and
applies the plan in the same atom.

    teammate / waiting   nothing changes hands: the Inbox is woken
    buddy                Buddy keeps it; the new message is pending work
                         (last_inbound_at > bot_cursor_at, a predicate)
    unattended           Buddy starts when an agent is set — else it waits
                         in the Inbox as Unassigned
    resolved / new       the thread reopens fresh (no session carried over,
                         D12), then follows the unattended rule
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.crm.conversations.schemas import Handoff, Thread
from app.crm.conversations.state import held_by
from app.crm.conversations.status import (
    HELD_BY_BUDDY,
    HELD_BY_NOBODY,
    HELD_RESOLVED,
)


@dataclass(frozen=True)
class InboundPlan:
    """The thread's controller fields after the message. ``bot_template_id``
    and ``bot_session_id`` are written as given — None clears them."""

    reopen: bool
    bot_template_id: Optional[str]
    bot_session_id: Optional[str]

    @property
    def buddy_answers(self) -> bool:
        return self.bot_template_id is not None


def plan_inbound(
    thread: Thread,
    handoff: Optional[Handoff],
    agent_id: Optional[str],
    claim_sla_minutes: int,
    now: datetime,
) -> InboundPlan:
    """PURE: see the module docstring."""
    held = held_by(thread, handoff, claim_sla_minutes, now)
    if held == HELD_RESOLVED:
        return InboundPlan(reopen=True, bot_template_id=agent_id, bot_session_id=None)
    if held == HELD_BY_NOBODY:
        return InboundPlan(reopen=False, bot_template_id=agent_id, bot_session_id=None)
    if held == HELD_BY_BUDDY:
        return InboundPlan(
            reopen=False,
            bot_template_id=thread.bot_template_id,
            bot_session_id=thread.bot_session_id,
        )
    # teammate or waiting: whoever holds it keeps it.
    return InboundPlan(
        reopen=False,
        bot_template_id=thread.bot_template_id,
        bot_session_id=thread.bot_session_id,
    )

"""Who holds a thread — DERIVED from the row (its open handoff is four
columns on it), never
stored as a word (a stored "who" is a second answer that drifts).

    resolved    resolved_at is set
    teammate    a teammate is assigned
    waiting     an agent asked for a person and nobody has claimed it yet,
                inside the claim SLA — Buddy is silent, "Needs attention"
    buddy       an agent is answering (bot_template_id)
    unattended  none of the above — Inbox "Unassigned"

A handoff past its claim SLA reads as lapsed here even before the sweep
closes it, so a stopped sweep never leaves a customer with nobody (the fork
lesson): Buddy takes the thread back the moment the SLA runs out.
"""

from datetime import datetime, timedelta

from app.crm.conversations.schemas import Thread
from app.crm.conversations.status import (
    HELD_BY_BUDDY,
    HELD_BY_NOBODY,
    HELD_BY_TEAMMATE,
    HELD_RESOLVED,
    HELD_WAITING,
)


def lapsed(thread: Thread, claim_sla_minutes: int, now: datetime) -> bool:
    """PURE: an open handoff nobody claimed inside the SLA."""
    return (
        thread.handoff_requested_at is not None
        and thread.assignee_user_id is None
        and thread.handoff_requested_at + timedelta(minutes=claim_sla_minutes) <= now
    )


def held_by(thread: Thread, claim_sla_minutes: int, now: datetime) -> str:
    """PURE: who holds the thread right now."""
    if thread.resolved_at is not None:
        return HELD_RESOLVED
    if thread.assignee_user_id is not None:
        return HELD_BY_TEAMMATE
    if thread.handoff_requested_at is not None and not lapsed(
        thread, claim_sla_minutes, now
    ):
        return HELD_WAITING
    if thread.bot_template_id is not None:
        return HELD_BY_BUDDY
    return HELD_BY_NOBODY

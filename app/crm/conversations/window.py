"""The free-form reply window — always a predicate, never stored (law 10).

A customer's message opens (or resets) the channel's window: the business
may send free-form messages until last_inbound_at + the channel's hours.
Our own sends never move it. The closing message is due ``lead`` minutes
before it shuts (R5).
"""

from datetime import datetime, timedelta
from typing import Optional


def closes_at(
    last_inbound_at: Optional[datetime], window_hours: int
) -> Optional[datetime]:
    """PURE: when the window shuts, or None when the customer never wrote."""
    if last_inbound_at is None:
        return None
    return last_inbound_at + timedelta(hours=window_hours)


def is_open(
    last_inbound_at: Optional[datetime], window_hours: int, now: datetime
) -> bool:
    """PURE: may we send free-form right now?"""
    end = closes_at(last_inbound_at, window_hours)
    return end is not None and now < end


def closing_due(
    last_inbound_at: Optional[datetime],
    window_hours: int,
    lead_minutes: int,
    now: datetime,
) -> bool:
    """PURE: is it time for the closing message — inside the lead, before
    the window shuts?"""
    end = closes_at(last_inbound_at, window_hours)
    if end is None:
        return False
    return end - timedelta(minutes=lead_minutes) <= now < end

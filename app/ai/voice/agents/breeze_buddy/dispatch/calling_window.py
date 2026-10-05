"""
Moves leads parked for calling hours when a template's calling window changes.

Parked leads sit at exactly the window start (``_calling_window_park_time``).
Other scheduled leads never land on that instant, so only parked leads move.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.ai.voice.agents.breeze_buddy.dispatch.queue import schedule_leads
from app.ai.voice.agents.breeze_buddy.managers.calls import (
    _calling_window_park_time,
    _is_within_calling_hours,
    _window_park_time,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.dispatch import wake_window_parked_leads
from app.schemas import CallExecutionConfig

IST = timezone(timedelta(hours=5, minutes=30))


def window_wake_time(config: CallExecutionConfig, now: datetime) -> datetime:
    """Now if the window is open, otherwise the next window start."""
    if _is_within_calling_hours(config, now):
        return now
    return _calling_window_park_time(config, now)


def parked_instants(config: CallExecutionConfig, now: datetime) -> List[datetime]:
    """Today's and tomorrow's window start under ``config``."""
    today = now.astimezone(IST).date()
    return [_window_park_time(config, today + timedelta(days=d)) for d in (0, 1)]


def _window_changed(old: CallExecutionConfig, new: CallExecutionConfig) -> bool:
    return (old.call_start_time, old.call_end_time) != (
        new.call_start_time,
        new.call_end_time,
    )


async def wake_leads_for_new_window(
    old: CallExecutionConfig,
    new: CallExecutionConfig,
    now: Optional[datetime] = None,
) -> int:
    """
    Move leads parked at the old window start to the new wake time, in the
    DB and on the schedule. Returns the number of leads moved.

    Best-effort: errors are logged, never raised. Leads not moved wake at
    the old start, and the worker checks the live window then.
    """
    template_id = new.template_id or old.template_id
    if not template_id or not _window_changed(old, new):
        return 0

    now = now or datetime.now(timezone.utc)
    wake_at = window_wake_time(new, now)
    try:
        moved = await wake_window_parked_leads(
            template_id=template_id,
            parked_at=parked_instants(old, now),
            wake_at=wake_at,
        )
    except Exception as e:  # noqa: BLE001 — config save must not fail on this
        logger.error(
            f"Calling window changed for template {template_id} but parked "
            f"leads were not moved; they wake at the old start: {e}"
        )
        return 0

    # The promoter reads the schedule, not the DB column.
    await schedule_leads(moved)

    if moved:
        logger.info(
            f"Calling window changed for template {template_id}: moved "
            f"{len(moved)} parked leads to {wake_at.isoformat()}"
        )
    return len(moved)

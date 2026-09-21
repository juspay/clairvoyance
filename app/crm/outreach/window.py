"""The calling window — when a waiting square's timer may move a run on.

A scheduling window on the PLAN's clock (the timezone the author wrote), not
the customer's, and not the quiet-hours control: nothing here knows where the
customer is. The dialler's calling hours remain the check on every call.

A plan that calls at night queues a lead the dialler cannot ring until
morning, and a run standing on that call hears nothing until it does. The
window keeps the run on its waiting square instead: the timer ends, the
hours are closed, so the alarm moves to the next opening and the square
keeps listening. A goal or a letter at night is then the plan's business
before any call exists.

Pure: the arithmetic is here; enrol() and the walker ask it where they set
or honour an alarm.
"""

from datetime import datetime, time, timedelta
from typing import Tuple
from zoneinfo import ZoneInfo

from app.crm.outreach.schemas import WaitWindow, WorkflowNode


def _clock(hh_mm: str) -> time:
    hours, minutes = hh_mm.split(":")
    return time(int(hours), int(minutes))


def opens_at(at: datetime, window: WaitWindow) -> datetime:
    """PURE: `at` itself when it falls inside the window, else the moment
    the window next opens after it. `closes` is exclusive; an `opens` later
    than `closes` spans midnight.

    `at` must carry its timezone: a naive time would be read on the server's
    own clock, which is exactly the ambiguity the window's `timezone` removes.

    Daylight saving, on a zone that has it: an `opens` that falls in the
    spring-forward gap opens at the first real instant after it (02:30 on the
    night 02:00 jumps to 03:00 opens at 03:30 local), and an `opens` in the
    repeated fall-back hour opens at its first occurrence."""
    if at.tzinfo is None:
        raise ValueError("opens_at needs an aware datetime (the walker holds UTC)")
    zone = ZoneInfo(window.timezone)
    local = at.astimezone(zone)
    start, end = _clock(window.opens), _clock(window.closes)
    now = local.time()
    inside = start <= now < end if start < end else (now >= start or now < end)
    if inside:
        return at
    opening = datetime.combine(local.date(), start, tzinfo=zone)
    if opening <= local:
        opening = datetime.combine(local.date() + timedelta(days=1), start, tzinfo=zone)
    return opening.astimezone(at.tzinfo)


# A listening wait with no minutes sleeps until the run's life ends. Its
# alarm lands just PAST that end, so the walker's max-age check ends the run
# as timed_out rather than the timeout arrow; the minute absorbs clock skew
# between the database that claims the run and the worker that judges it.
_PAST_THE_END = timedelta(minutes=1)


def last_opening(at: datetime, window: WaitWindow) -> datetime:
    """PURE: the moment the window last opened at or before `at` — the
    start of the drain the progress line counts from (progress.py): the
    pile is what the window held since it closed, and it starts moving
    when the window opens."""
    if at.tzinfo is None:
        raise ValueError("last_opening needs an aware datetime")
    zone = ZoneInfo(window.timezone)
    local = at.astimezone(zone)
    opening = datetime.combine(local.date(), _clock(window.opens), tzinfo=zone)
    if opening > local:
        opening = datetime.combine(
            local.date() - timedelta(days=1), _clock(window.opens), tzinfo=zone
        )
    return opening.astimezone(at.tzinfo)


def held_alarm(
    node: WorkflowNode, now: datetime, run_ends_at: datetime
) -> Tuple[datetime, bool]:
    """PURE: ``alarm`` and whether the window MOVED it — the one fact the
    run's lane is made of (the overnight drain, 21 Sep 2026). A timer that
    ends inside the hours keeps its moment and the run keeps its lane; one
    the window holds to the next opening makes the run cold, and the
    walker then feeds it to the dialler only into the lines the plan's
    numbers have free (capacity.py). A letter never comes through here, so
    a letter never makes a run cold."""
    if node.minutes:
        wake = now + timedelta(minutes=node.minutes)
    elif node.topics:
        wake = max(run_ends_at, now) + _PAST_THE_END
    else:
        wake = now
    if node.window is None:
        return wake, False
    opening = opens_at(wake, node.window)
    return opening, opening != wake


def alarm(node: WorkflowNode, now: datetime, run_ends_at: datetime) -> datetime:
    """PURE: a wait's alarm on arrival (minutes optional, ruled 17 Sep 2026).
    Two questions, composed rather than branched: HOW LONG, then WHEN THAT
    MAY ACT.
    - How long: its minutes; else, for a listening wait, just past the end
      of the run's life (`run_ends_at`) — "listen as long as this run may
      live"; else none — a bare window waits only for the hours.
    - When: a window moves that moment to its next opening when it falls
      outside the hours (unchanged when it is inside).
    So a listening wait with a window and no minutes listens for the run's
    life and acts at the next opening after it — it never fires on arrival
    just because the hours are open. Publish refuses a wait with none of
    minutes, window or topics."""
    return held_alarm(node, now, run_ends_at)[0]

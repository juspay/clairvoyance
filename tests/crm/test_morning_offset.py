"""The morning offset (22 Sep 2026; docs/crm/runbooks/morning-offset.md).

A timer set inside a window's reserved minutes after the opening runs the
offset longer, so the runs the window held overnight reach the dialler
first and the day's live customers queue behind them, not beside them.
Pinned against `window.alarm`, the one place a wait's alarm is computed.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.crm.outreach.schemas import WaitWindow, WorkflowNode
from app.crm.outreach.window import alarm, in_reserved_period

IST = {"opens": "09:00", "closes": "21:00", "timezone": "Asia/Kolkata"}


def _window(offset: int) -> WaitWindow:
    return WaitWindow.model_validate({**IST, "held_runs_first_minutes": offset})


def _wait(minutes: int, offset: int) -> WorkflowNode:
    return WorkflowNode.model_validate(
        {
            "id": "quiet",
            "type": "wait",
            "minutes": minutes,
            "window": {**IST, "held_runs_first_minutes": offset},
        }
    )


def _ist(hh: int, mm: int, day: int = 22) -> datetime:
    return datetime(2026, 9, day, hh, mm, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(
        timezone.utc
    )


def test_a_window_without_the_word_has_no_reserved_period() -> None:
    assert WaitWindow.model_validate(IST).held_runs_first_minutes == 0
    assert not in_reserved_period(_ist(9, 30), WaitWindow.model_validate(IST))


def test_the_reserved_period_is_the_offset_after_the_opening() -> None:
    window = _window(60)
    assert in_reserved_period(_ist(9, 0), window)
    assert in_reserved_period(_ist(9, 59), window)
    assert not in_reserved_period(_ist(10, 0), window)
    assert not in_reserved_period(_ist(8, 59), window)
    assert not in_reserved_period(_ist(9, 30), _window(0))  # no offset


def test_a_timer_set_in_the_reserved_period_runs_the_offset_longer() -> None:
    end = _ist(9, 0, 30)
    # Ravi enters at 09:20; quiet 15 would end 09:35; the offset makes it 10:35.
    assert alarm(_wait(15, 60), _ist(9, 20), end) == _ist(10, 35)
    # A held run's call ended 09:12; its gap 30 ends 09:42 -> 10:42.
    assert alarm(_wait(30, 60), _ist(9, 12), end) == _ist(10, 42)
    # After 10:00 nothing is added.
    assert alarm(_wait(15, 60), _ist(10, 5), end) == _ist(10, 20)
    # Without an offset, 09:20 + 15 is 09:35 as always.
    assert alarm(_wait(15, 0), _ist(9, 20), end) == _ist(9, 35)


def test_the_hold_still_applies_after_the_offset() -> None:
    # 09:30 + (11 h + 60 min offset) lands past 21:00: held to tomorrow's opening.
    assert alarm(_wait(11 * 60, 60), _ist(9, 30), _ist(9, 0, 30)) == _ist(9, 0, 23)


def test_the_night_pile_still_wakes_at_the_opening() -> None:
    # Set at 23:00 the night before, outside the reserved period: no offset,
    # the window's hold alone -> 09:00, the pile.
    assert alarm(_wait(15, 60), _ist(23, 0, 21), _ist(9, 0, 30)) == _ist(9, 0, 22)


def test_a_listening_wait_with_no_timer_is_untouched_by_the_offset() -> None:
    node = WorkflowNode.model_validate(
        {
            "id": "listen",
            "type": "wait",
            "topics": ["OFFERED"],
            "key": "$topic",
            "window": {**IST, "held_runs_first_minutes": 60},
        }
    )
    with_offset = alarm(node, _ist(9, 20), _ist(9, 0, 30))
    node.window = WaitWindow.model_validate(IST)
    assert with_offset == alarm(node, _ist(9, 20), _ist(9, 0, 30))

"""The calling window: when a waiting square's timer may move a run on.

A plan that queues a call at night hands the dialler a lead it cannot ring
until morning, and the run standing on the call hears nothing until then.
A window keeps the run on its waiting square instead: the timer ends
outside the hours, so the alarm moves to the next opening and the square
keeps listening. These tests pin the arithmetic (window.py), the shape
(schemas.WaitWindow), the publish law, and enrol()'s first alarm."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

from app.crm.outreach.enrol import _first_wake
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import WaitWindow, WorkflowNode
from app.crm.outreach.window import alarm, opens_at

IST = timezone(timedelta(hours=5, minutes=30))
CALLING_HOURS = WaitWindow.model_validate(
    {"opens": "07:00", "closes": "23:00", "timezone": "Asia/Kolkata"}
)
OVERNIGHT = WaitWindow.model_validate(
    {"opens": "22:00", "closes": "06:00", "timezone": "Asia/Kolkata"}
)


def _ist(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment on the IST clock, handed over in UTC as the walker holds it."""
    return datetime(2026, 9, day, hour, minute, tzinfo=IST).astimezone(timezone.utc)


@pytest.mark.parametrize(
    "at, expected",
    [
        (_ist(17, 12), _ist(17, 12)),  # inside: no change
        (_ist(17, 7), _ist(17, 7)),  # `from` is inside
        (_ist(17, 3), _ist(17, 7)),  # before the opening: today's
        (_ist(17, 23), _ist(18, 7)),  # `to` is outside: tomorrow's
        (_ist(17, 23, 40), _ist(18, 7)),
    ],
)
def test_a_moment_outside_the_hours_moves_to_the_next_opening(
    at: datetime, expected: datetime
) -> None:
    assert opens_at(at, CALLING_HOURS) == expected
    assert opens_at(at, CALLING_HOURS).tzinfo == timezone.utc


@pytest.mark.parametrize(
    "at, expected",
    [
        (_ist(17, 23), _ist(17, 23)),
        (_ist(18, 3), _ist(18, 3)),
        (_ist(17, 12), _ist(17, 22)),
        (_ist(17, 6), _ist(17, 22)),
    ],
)
def test_a_window_from_later_than_to_spans_midnight(
    at: datetime, expected: datetime
) -> None:
    assert opens_at(at, OVERNIGHT) == expected


def test_the_window_reads_its_own_clock_not_utc() -> None:
    """20:00 UTC is 01:30 IST: closed, though 20:00 is inside 07:00-23:00."""
    at = datetime(2026, 9, 16, 20, 0, tzinfo=timezone.utc)
    assert opens_at(at, CALLING_HOURS) == _ist(17, 7)


def test_an_alarm_is_arrival_plus_minutes_moved_into_the_hours() -> None:
    quiet = WorkflowNode(id="quiet-15m", type="wait", minutes=15)
    held = WorkflowNode(id="quiet-15m", type="wait", minutes=15, window=CALLING_HOURS)
    ends = _ist(20, 0)  # the run's life; minutes decide first
    assert alarm(quiet, _ist(17, 22, 50), ends) == _ist(17, 23, 5)
    assert alarm(held, _ist(17, 22, 50), ends) == _ist(18, 7)
    assert alarm(held, _ist(17, 9), ends) == _ist(17, 9, 15)  # the day is untouched


def test_a_run_starting_on_a_windowed_wait_first_wakes_at_the_opening() -> None:
    start = WorkflowNode(id="quiet", type="wait", minutes=15, window=CALLING_HOURS)
    # held to the opening: the run is born COLD (migration 077)
    assert _first_wake(start, _ist(17, 23, 30), 7) == (_ist(18, 7), "cold")


@pytest.mark.parametrize(
    "window, words",
    [
        ({"opens": "7:00", "closes": "23:00", "timezone": "Asia/Kolkata"}, "opens"),
        ({"opens": "07:00", "closes": "24:00", "timezone": "Asia/Kolkata"}, "closes"),
        (
            {"opens": "07:00", "closes": "07:00", "timezone": "Asia/Kolkata"},
            "must differ",
        ),
        ({"opens": "07:00", "closes": "23:00", "timezone": "Mars/Olympus"}, "timezone"),
        ({"opens": "07:00", "closes": "23:00"}, "timezone"),
    ],
)
def test_a_window_that_cannot_open_is_refused(
    window: Dict[str, Any], words: str
) -> None:
    with pytest.raises(ValueError, match=words):
        WaitWindow.model_validate(window)


def _plan(node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "entry": {"topic": "INITIATED"},
        "nodes": [node, {"id": "after", "type": "wait", "minutes": 60}],
        "edges": [[node["id"], "after"]],
        "goals": [{"topics": ["GRANTED"]}],
    }


def test_only_a_waiting_square_takes_a_window() -> None:
    hours = {"opens": "07:00", "closes": "23:00", "timezone": "Asia/Kolkata"}
    call = {"id": "call-1", "type": "call", "template_id": "tpl-1", "window": hours}
    wait = {"id": "quiet", "type": "wait", "minutes": 15, "window": hours}
    assert any(
        "window belongs to a wait" in p for p in validate_definition(_plan(call))
    )
    assert not any("window" in p for p in validate_definition(_plan(wait)))


def test_a_moment_without_a_timezone_is_refused() -> None:
    """A naive time would be read on the server's clock — the ambiguity the
    window's own timezone exists to remove."""
    with pytest.raises(ValueError, match="aware"):
        opens_at(datetime(2026, 9, 17, 3, 0), CALLING_HOURS)


@pytest.mark.parametrize(
    "hours, at, expected",
    [
        # 2026-03-08 02:00 EST jumps to 03:00 EDT: a 02:30 opening is the first
        # real instant after the gap, 03:30 EDT (07:30 UTC).
        (
            {"opens": "02:30", "closes": "05:00"},
            datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc),  # 01:00 EST
            datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc),
        ),
        # 2026-11-01 01:00-02:00 happens twice: 01:30 opens at its first
        # occurrence, 01:30 EDT (05:30 UTC).
        (
            {"opens": "01:30", "closes": "04:00"},
            datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc),  # 00:00 EDT
            datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc),
        ),
    ],
    ids=["spring-forward gap", "fall-back repeat"],
)
def test_daylight_saving_opens_at_the_first_real_instant(
    hours: Dict[str, str], at: datetime, expected: datetime
) -> None:
    window = WaitWindow.model_validate({**hours, "timezone": "America/New_York"})
    assert opens_at(at, window) == expected


# --- the publish law: a letter from a windowed square may not reach a call ---

_HOURS = {"opens": "09:00", "closes": "17:00", "timezone": "Asia/Kolkata"}
_HEARS = {"type": "wait", "topics": ["checkout.updated"], "key": "$topic"}


def _ladder(letter: list, rule_yes: str = "quiet", window: bool = True) -> Dict:
    """quiet (windowed) -> call-1 on its timer; the letter arrow is the case
    under test; rule's yes lands on quiet unless said otherwise."""
    quiet: Dict[str, Any] = {"id": "quiet", "minutes": 15, **_HEARS}
    if window:
        quiet["window"] = _HOURS
    return {
        "entry": {"topic": "checkout.initiated"},
        "nodes": [
            quiet,
            {"id": "call-1", "type": "call", "template_id": "tpl-1"},
            {"id": "listen", "minutes": 1440, **_HEARS},
            {
                "id": "rule",
                "type": "condition",
                "rules": [
                    {"on": "yes", "if": [{"field": "context.offers", "op": "exists"}]}
                ],
            },
        ],
        "edges": [
            ["quiet", "call-1", "timeout"],
            letter,
            ["call-1", "listen"],
            ["listen", "rule", "checkout.updated"],
            ["rule", rule_yes, "yes"],
            ["rule", "listen", "else"],
        ],
        "goals": [{"topics": ["order.placed"]}],
    }


def _window_problems(doc: Dict[str, Any]) -> list:
    return [p for p in validate_definition(doc) if "has a window" in p]


def test_a_letter_arrow_straight_onto_a_call_is_refused() -> None:
    """The review's case: one edge, quiet -> call-1 on a letter, and a call is
    queued in the shut hours."""
    (problem,) = _window_problems(_ladder(["quiet", "call-1", "checkout.updated"]))
    assert "quiet" in problem and "'checkout.updated'" in problem
    assert "call-1" in problem


def test_a_letter_that_reaches_a_call_through_a_condition_is_refused_too() -> None:
    doc = _ladder(["quiet", "rule", "checkout.updated"], rule_yes="call-1")
    (problem,) = _window_problems(doc)
    assert "reaches call call-1 without waiting" in problem


def test_an_else_arrow_is_a_letter_arrow() -> None:
    """`else` also catches letters, so it may not lead to a call either."""
    doc = _ladder(["quiet", "call-1", "else"])
    doc["edges"] = [e for e in doc["edges"] if e[2:] != ["timeout"]]
    assert len(_window_problems(doc)) == 1


def test_a_letter_back_to_the_rule_that_waits_again_is_accepted() -> None:
    """The Flipkart ladder's shape: the letter goes back to the rule, whose
    arrows land on waits — the call is only reached by quiet's timer."""
    assert _window_problems(_ladder(["quiet", "rule", "checkout.updated"])) == []


def test_without_a_window_nothing_is_promised_and_nothing_refused() -> None:
    doc = _ladder(["quiet", "call-1", "checkout.updated"], window=False)
    assert _window_problems(doc) == []


def test_a_plain_waits_single_arrow_is_its_timer() -> None:
    doc = _plan({"id": "quiet", "type": "wait", "minutes": 15, "window": _HOURS})
    doc["nodes"][1] = {"id": "after", "type": "call", "template_id": "tpl-1"}
    assert _window_problems(doc) == []

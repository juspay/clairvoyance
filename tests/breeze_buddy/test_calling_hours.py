"""
Calling-hours rule (``managers/calls.py``): the window is in IST, inclusive at
both ends, and wraps past midnight when start > end. The v2 dialler's Lua
copies this rule; ``tests/breeze_buddy/dispatch/v2/test_scripts.py`` checks
the two agree.
"""

from datetime import datetime, time, timezone
from types import SimpleNamespace
from typing import Any

import pytest

# Importing the dispatch package first initializes worker -> managers.calls in
# the supported order (see test_number_picker.py).
import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
import app.ai.voice.agents.breeze_buddy.managers.calls as calls_mod


@pytest.mark.parametrize(
    "start,end,now,open_",
    [
        (time(9), time(21), time(9), True),  # inclusive start
        (time(9), time(21), time(21), True),  # inclusive end
        (time(9), time(21), time(21, 0, 0, 1), False),  # just past the end
        (time(9), time(21), time(8, 59, 59), False),
        (time(22), time(6), time(1), True),  # wraps past midnight
        (time(22), time(6), time(6), True),
        (time(22), time(6), time(12), False),
    ],
)
def test_hours_open(start, end, now, open_):
    assert calls_mod.hours_open(start, end, now) is open_


def test_is_within_calling_hours_reads_ist_now(monkeypatch):
    at_2100_ist = datetime(2026, 10, 4, 15, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        calls_mod,
        "datetime",
        SimpleNamespace(now=lambda tz=None: at_2100_ist.astimezone(tz)),
    )
    open_until_21: Any = SimpleNamespace(
        call_start_time=time(9), call_end_time=time(21)
    )
    open_until_2059: Any = SimpleNamespace(
        call_start_time=time(9), call_end_time=time(20, 59)
    )
    assert calls_mod._is_within_calling_hours(open_until_21) is True
    assert calls_mod._is_within_calling_hours(open_until_2059) is False

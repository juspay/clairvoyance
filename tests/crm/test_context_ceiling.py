"""The run-context ceiling and the engine's join budget are ONE dial in two
files, and neither file may import the other (app/core sits under app/crm).
This is where that inequality is executable.

The pair matters because join_list truncates a joined list TO the engine's
budget precisely so the scalar gate cannot drop it whole — "the send would
then park on a blank whose cause is two modules away". A ceiling below the
budget re-opens exactly that failure, silently, for every full-length cart.
"""

import pytest

from app.core.config import dynamic
from app.crm.record.extractors import engine


@pytest.fixture(autouse=True)
def _cold_cache():
    """Each case starts with no process-local copy, and leaves none behind —
    the cache is a module global and would otherwise leak across tests."""
    dynamic._context_value_cache = None
    yield
    dynamic._context_value_cache = None


def _stored(monkeypatch, value, calls=None):
    async def fake_get_config(key, default_value, return_type=str):
        assert key == "CRM_CONTEXT_VALUE_MAX_CHARS"
        if calls is not None:
            calls.append(key)
        return value

    monkeypatch.setattr(dynamic, "get_config", fake_get_config)


def test_the_floor_is_the_engine_s_join_budget() -> None:
    """The pin. app/core imports nothing from app.crm, so the constant is
    duplicated rather than imported — and duplicated constants drift unless
    something fails when they do. This is that something."""
    assert dynamic._CONTEXT_VALUE_FLOOR == engine.VARIABLE_MAX_CHARS


@pytest.mark.asyncio
async def test_an_operator_may_raise_the_ceiling(monkeypatch) -> None:
    """The whole reason the dial is live: one noisy producer needs more room
    and should not need a deploy to get it."""
    _stored(monkeypatch, 1024)
    assert await dynamic.CRM_CONTEXT_VALUE_MAX_CHARS() == 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [200, 0, -1])
async def test_a_ceiling_below_the_join_budget_is_clamped(monkeypatch, stored) -> None:
    """Lowering it is the one move that breaks the pair. At 200 every
    full-length joined cart is dropped from context; at 0 every run is born
    empty. The operator gets the floor and a log line, not the failure."""
    _stored(monkeypatch, stored)
    assert await dynamic.CRM_CONTEXT_VALUE_MAX_CHARS() == engine.VARIABLE_MAX_CHARS


@pytest.mark.asyncio
async def test_an_unreadable_value_falls_back_to_the_floor(monkeypatch) -> None:
    """A dial set to "wide" in Redis must not raise out of the consumer
    pass: the letter is not at fault for the operator's typo."""
    _stored(monkeypatch, "wide")
    assert await dynamic.CRM_CONTEXT_VALUE_MAX_CHARS() == engine.VARIABLE_MAX_CHARS


@pytest.mark.asyncio
async def test_the_ceiling_is_read_once_per_window_not_once_per_letter(
    monkeypatch,
) -> None:
    """This is the spine's per-row path — entry.py reads it for every
    consumed letter, in both enrol and the wait-event branch. Uncached that
    is a Redis GET per event on the busiest worker in the system, for a
    value that changes about once a year."""
    calls: list = []
    _stored(monkeypatch, 512, calls)

    for _ in range(50):
        assert await dynamic.CRM_CONTEXT_VALUE_MAX_CHARS() == 512
    assert len(calls) == 1, calls

    # The window is what makes it live rather than static: when it lapses,
    # the next letter sees the operator's new value.
    dynamic._context_value_cache = None
    _stored(monkeypatch, 900, calls)
    assert await dynamic.CRM_CONTEXT_VALUE_MAX_CHARS() == 900
    assert len(calls) == 2, calls

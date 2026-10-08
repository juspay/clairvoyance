"""Fixtures shared by every Breeze Buddy test."""

import time

import pytest


@pytest.fixture(autouse=True)
def _v2_latch_unseen():
    """Every test starts with the v2 latch unseen and just checked, so today's path does
    no v2 read until a test turns v2 on itself (tests of the latch reset it). The latch is
    a process global that stays true once set: without this, a v2 test that set it would
    put the next test's "today's path" on v2 hooks (Fable M8)."""
    # lazy: importing the dispatch package at collection time would change the import
    # order the test modules rely on (managers.calls <-> dispatch cycle)
    from app.ai.voice.agents.breeze_buddy.dispatch.v2 import latch

    latch._reset_for_tests()
    latch._checked_at = time.monotonic()
    yield
    latch._reset_for_tests()

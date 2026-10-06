"""The v2 latch is a process global that, once true, stays true (design card rule 23).
No test may leave it set for the next one: today's-path tests would run v2 hooks (Fable
M8). The two tests below run in file order; the first leaks the latch on purpose."""

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import latch


def test_a_test_that_sees_v2_and_does_not_clean_up():
    latch._seen = True  # deliberately not via monkeypatch: nothing undoes it here


def test_the_next_test_starts_with_v2_unseen():
    assert latch._seen is False

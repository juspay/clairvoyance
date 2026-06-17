"""Regression tests for the user-turn-stop strategy used in timeout/stt_native
turn detection.

History: a local ``AccumulatingSpeechTimeoutStrategy`` subclass patched a bug in
pipecat's old single-timer ``SpeechTimeoutUserTurnStopStrategy`` by re-seeding a
sentinel timer in ``setup()``/``reset()`` (it poked ``self._timeout_task``).

pipecat 1.1.0 rewrote that base class into a two-timer design with no
``_timeout_task``/``_timeout_handler``. The subclass's ``_seed_sentinel`` only
ran when ``user_speech_timeout > 0`` (it short-circuited at 0.0), so every
``stt_native`` template (timeout 0.0) was unaffected — but the moment a template
switched to ``turn_detection: timeout`` with a non-zero ``user_speech_timeout``,
``setup()``/``reset()`` raised ``AttributeError`` on the missing
``_timeout_task`` and broke the turn lifecycle: the user turn never stopped, so
the LLM was never invoked and the bot listened forever.

Fix: drop the subclass and use the base ``SpeechTimeoutUserTurnStopStrategy``
directly (it now handles the no-VAD multi-turn case natively). These tests lock
that in.
"""

from __future__ import annotations

import asyncio

from pipecat.frames.frames import TranscriptionFrame
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams

from app.ai.voice.agents.breeze_buddy.template import interruption


def _make_task_manager() -> TaskManager:
    tm = TaskManager()
    tm.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    return tm


def _transcription(text: str = "hello", finalized: bool = True) -> TranscriptionFrame:
    return TranscriptionFrame(
        user_id="user",
        text=text,
        timestamp="2026-06-18T00:00:00Z",
        finalized=finalized,
    )


def test_accumulating_subclass_is_removed():
    """The obsolete subclass must not come back — it crashes on pipecat 1.1.0."""
    assert not hasattr(interruption, "AccumulatingSpeechTimeoutStrategy")


def test_base_strategy_lacks_old_single_timer_api():
    """Guards the root cause: the old `_timeout_task` attribute is gone, so any
    code re-seeding it (the deleted `_seed_sentinel`) would AttributeError."""
    strat = SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=1.0)
    assert not hasattr(strat, "_timeout_task")
    assert hasattr(strat, "_user_speech_timeout_task")


async def test_setup_and_reset_do_not_raise_with_positive_timeout():
    """The exact crash we fixed: setup()+reset() with user_speech_timeout > 0.

    The deleted subclass raised AttributeError here; the base class must not.
    """
    strat = SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=1.0)
    await strat.setup(_make_task_manager())
    await strat.reset()  # called by the turn controller on every turn start/stop
    await strat.cleanup()


async def test_fires_after_timeout_in_no_vad_fallback():
    """A finalized transcript with no VAD stop must end the turn after
    user_speech_timeout seconds of silence (the timeout-mode behavior the
    template change wanted)."""
    fired = asyncio.Event()
    strat = SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.05)
    strat.add_event_handler("on_user_turn_stopped", lambda *_a, **_k: fired.set())
    await strat.setup(_make_task_manager())

    await strat.process_frame(_transcription(finalized=True))
    # Not fired immediately — the policy floor must elapse first.
    assert not fired.is_set()

    await asyncio.wait_for(fired.wait(), timeout=1.0)
    await strat.cleanup()


async def test_timer_rearms_on_each_transcript():
    """While the user keeps producing transcripts inside the window, the turn
    must NOT end — the timer rearms on every new transcript."""
    fired = asyncio.Event()
    strat = SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.15)
    strat.add_event_handler("on_user_turn_stopped", lambda *_a, **_k: fired.set())
    await strat.setup(_make_task_manager())

    # Three transcripts spaced under the timeout — each rearms the timer.
    for _ in range(3):
        await strat.process_frame(_transcription(finalized=True))
        await asyncio.sleep(0.05)
        assert not fired.is_set()

    # Now go quiet — the turn ends.
    await asyncio.wait_for(fired.wait(), timeout=1.0)
    await strat.cleanup()


async def test_fires_quickly_with_zero_timeout_stt_native():
    """stt_native uses user_speech_timeout=0.0 and must still end the turn
    right after a finalized transcript (behavior-preserving vs the old code)."""
    fired = asyncio.Event()
    strat = SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.0)
    strat.add_event_handler("on_user_turn_stopped", lambda *_a, **_k: fired.set())
    await strat.setup(_make_task_manager())

    await strat.process_frame(_transcription(finalized=True))
    await asyncio.wait_for(fired.wait(), timeout=1.0)
    await strat.cleanup()

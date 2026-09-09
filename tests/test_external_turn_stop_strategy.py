"""Turn-stop strategy selection when the STT detects turn boundaries itself.

Since pipecat 1.5 a turn-detecting STT (Soniox with
``vad_force_turn_endpoint=False``) finalizes transcripts PER SEGMENT while the
user is still talking, and announces the real end of turn separately as
``ProposedUserStoppedSpeakingFrame``. ``SpeechTimeoutUserTurnStopStrategy``
fires on transcript arrival, so on such a service it ends the turn on the first
segment: "So" / "can you" / "am I audible" became three user turns of one
sentence, and the bot answered fragments (observed on a live call, 2026-09-04).

The fix is ``ExternalUserTurnStopStrategy``, which waits for the service's
proposal. These tests lock in BOTH halves — the capability probe that picks it,
and the node-transition rebuild, which reverted to the fragmenting strategy at
the first node change even after the pipeline chose correctly.
"""

from __future__ import annotations

from types import SimpleNamespace

from pipecat.turns.user_stop import (
    ExternalUserTurnStopStrategy,
    SpeechTimeoutUserTurnStopStrategy,
)

from app.ai.voice.agents.breeze_buddy.template.interruption import (
    _build_stop_strategies,
)
from app.ai.voice.stt.turn_capability import stt_proposes_turn_boundaries


class _ProposingSTT:
    """An STT that advertises its own turn detection, like Soniox does."""

    def service_metadata_frame(self):
        return SimpleNamespace(user_turn_strategies=object())


class _SilentSTT:
    """An STT that proposes no turns (Sarvam, Google, OpenAI …)."""

    def service_metadata_frame(self):
        return SimpleNamespace(user_turn_strategies=None)


class _BrokenSTT:
    def service_metadata_frame(self):
        raise RuntimeError("probe blew up")


def test_probe_detects_turn_proposing_service():
    assert stt_proposes_turn_boundaries(_ProposingSTT()) is True


def test_probe_returns_false_for_non_proposing_service():
    assert stt_proposes_turn_boundaries(_SilentSTT()) is False


def test_probe_fails_closed():
    """A missing or broken service keeps the locally pinned strategy.

    Choosing the external strategy for a service that never proposes would hang
    every turn — the turn would stay open forever and the LLM never run.
    """
    assert stt_proposes_turn_boundaries(None) is False
    assert stt_proposes_turn_boundaries(_BrokenSTT()) is False


def test_node_transition_keeps_external_strategy():
    """The regression: node transitions rebuild strategies from scratch.

    Without this, the pipeline installs the external strategy at call start and
    the first node change silently swaps it back to the fragmenting one.
    """
    bot = SimpleNamespace(stt_proposes_turns=True)
    strategies = _build_stop_strategies(bot, user_speech_timeout=0.0)
    assert isinstance(strategies[0], ExternalUserTurnStopStrategy)


def test_node_transition_keeps_timeout_strategy_without_proposals():
    bot = SimpleNamespace(stt_proposes_turns=False)
    strategies = _build_stop_strategies(bot, user_speech_timeout=0.0)
    assert isinstance(strategies[0], SpeechTimeoutUserTurnStopStrategy)


def test_node_speech_timeout_overrides_external():
    """A node asking to accumulate multi-segment input keeps its timeout.

    Its timer rearms on every new transcript, so per-segment finals are
    harmless there — the node's explicit choice wins.
    """
    bot = SimpleNamespace(stt_proposes_turns=True)
    strategies = _build_stop_strategies(bot, user_speech_timeout=1.5)
    assert isinstance(strategies[0], SpeechTimeoutUserTurnStopStrategy)


def test_bot_without_the_flag_falls_back_safely():
    """An older bot object (no attribute) must not crash a node transition."""
    strategies = _build_stop_strategies(SimpleNamespace(), user_speech_timeout=0.0)
    assert isinstance(strategies[0], SpeechTimeoutUserTurnStopStrategy)

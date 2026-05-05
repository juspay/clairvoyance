"""Chat-mode handler/function filter list.

Names listed here are voice-only (or not yet implemented for chat) and are
stripped from per-node functions, global functions, and per-node pre/post
actions before pydantic validation. Stripping (rather than noop-swapping)
keeps a single ``handler_map`` for both channels and prevents the LLM from
ever seeing these functions as callable.

Passed into ``FlowConfigBuilder`` from ``ChatAgent.run_turn`` — the builder
itself is channel-agnostic and just consumes the set.

Why each entry is disabled:

- ``mute_stt`` / ``unmute_stt`` / ``play_audio_sound``: voice-only side
  effects (no STT or audio output exists in the chat pipeline).
- ``warm_transfer`` / ``connect_to_live_agent``: chat-to-human handoff is
  not implemented in v1; see ``docs/CHAT_MODE.md §15`` for the Phase 2
  plan.
- ``end_conversation``: voice templates wire this as a post-action on the
  closing node to push ``EndFrame()`` and tear down the long-lived call
  pipeline. Chat agents are constructed-and-discarded per turn — the
  pipeline tears down via ``run_turn``'s ``finally`` already, and queueing
  an ``EndFrame`` mid-turn races the just-queued ``LLMRunFrame`` for the
  closing node and cancels the user-facing reply in flight.
"""

from __future__ import annotations

CHAT_DISABLED_NAMES: frozenset[str] = frozenset(
    {
        "mute_stt",
        "unmute_stt",
        "play_audio_sound",
        "warm_transfer",
        "connect_to_live_agent",
        "end_conversation",
    }
)


__all__ = ["CHAT_DISABLED_NAMES"]

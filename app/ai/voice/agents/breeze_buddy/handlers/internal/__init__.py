"""
Handler functions for template.

This package contains all handler functions organized by category.
"""

from app.ai.voice.agents.breeze_buddy.handlers.internal.audio import play_audio_sound
from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
    end_conversation,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.stt import (
    mute_stt,
    unmute_stt,
)

__all__ = [
    "end_conversation",
    "mute_stt",
    "play_audio_sound",
    "unmute_stt",
]

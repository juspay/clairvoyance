"""
Handler functions for template.

This package contains all handler functions organized by category.
"""

from app.ai.voice.agents.breeze_buddy.handlers.internal.audio import play_audio_sound
from app.ai.voice.agents.breeze_buddy.handlers.internal.builtin_dispatcher import (
    builtin_function_dispatcher,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.custom_python_code_handler import (
    custom_python_code_handler,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
    end_conversation,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.get_current_time import (
    get_current_time,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.send_whatsapp_message import (
    send_whatsapp_message,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.stt import (
    mute_stt,
    unmute_stt,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.update_outcome import (
    update_outcome,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.warm_transfer import (
    connect_to_live_agent,
)

__all__ = [
    "builtin_function_dispatcher",
    "connect_to_live_agent",
    "custom_python_code_handler",
    "end_conversation",
    "get_current_time",
    "mute_stt",
    "play_audio_sound",
    "send_whatsapp_message",
    "unmute_stt",
    "update_outcome",
]

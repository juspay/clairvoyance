"""
Breeze Buddy Service Wrappers

This module contains custom service wrappers for the Breeze Buddy voice agent.
"""

from app.ai.voice.agents.breeze_buddy.services.llm_wrapper import (
    BreezeBuddyLLMWrapper,
)

__all__ = [
    "BreezeBuddyLLMWrapper",
]

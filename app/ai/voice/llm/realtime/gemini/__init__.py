"""Google Gemini Live realtime provider.

``realtime.py`` builds the pipecat ``GeminiLiveLLMService`` (Developer API)
from BB's ``LLMConfiguration``; ``opening_line.py`` pre-generates the
template's opening line at dispatch time with a throwaway Live session.
"""

from __future__ import annotations

from .realtime import (
    BuddyGeminiLiveLLMService,
    GeminiRealtimeConfig,
    build_gemini_realtime_llm,
    has_realtime_llm,
)

__all__ = [
    "BuddyGeminiLiveLLMService",
    "GeminiRealtimeConfig",
    "build_gemini_realtime_llm",
    "has_realtime_llm",
]

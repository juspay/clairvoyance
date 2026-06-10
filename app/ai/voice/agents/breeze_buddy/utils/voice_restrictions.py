"""Voice-restricted tool definitions for Breeze Buddy.

Tools listed here are available in chat mode but not in voice mode
because they are long-running agentic loops that would exhaust the
LLM context window during a real-time voice conversation.
"""

from typing import FrozenSet

# Tools that are only available in chat mode.
# When the LLM invokes one of these during a voice session, the agent
# should speak a redirect message and emit a 'voice-to-chat-redirect'
# RTVI event to the Lighthouse frontend.
VOICE_RESTRICTED_TOOLS: FrozenSet[str] = frozenset(
    [
        "initiateAgenticLoop",
        "generateAdsPerformanceReport",
        "generateShopifyBreezeGoLiveReport",
        "runSRAnalysis",
        "generateReport",
    ]
)

VOICE_REDIRECT_TTS_MESSAGE = (
    "This feature is only available in chat mode. "
    "I'll take you there now so you can get the full analysis."
)

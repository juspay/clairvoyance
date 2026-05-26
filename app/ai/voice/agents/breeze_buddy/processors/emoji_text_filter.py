"""Emoji text filter for TTS services.

Strips emoji and pictographic symbols from LLM-generated text before it is
sent to any TTS provider.  Without this filter, emoji characters cause TTS
services to either speak the codepoint name aloud (e.g. "grinning face") or
produce garbled audio.
"""

import re

from pipecat.utils.text.base_text_filter import BaseTextFilter

__all__ = ["EmojiTextFilter", "strip_emoji"]

# Unicode ranges that cover the standard emoji / pictographic blocks.
# Using a single compiled pattern for performance.
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f680-\U0001f6ff"  # transport & map symbols
    "\U0001f1e0-\U0001f1ff"  # regional indicator symbols (flags)
    "\U00002700-\U000027bf"  # dingbats
    "\U000024c2-\U0001f251"  # enclosed characters / misc symbols
    "\U0001f900-\U0001f9ff"  # supplemental symbols & pictographs
    "\U0001fa00-\U0001fa6f"  # chess symbols, medical symbols
    "\U0001fa70-\U0001faff"  # food, drink, household symbols
    "\U00002600-\U000026ff"  # miscellaneous symbols (☀ ☁ ☎ etc.)
    "]+",
    flags=re.UNICODE,
)


def strip_emoji(text: str) -> str:
    """Remove all emoji characters from *text* and collapse extra whitespace."""
    return _EMOJI_PATTERN.sub("", text).strip()


class EmojiTextFilter(BaseTextFilter):
    """Pipecat ``BaseTextFilter`` that removes emoji from TTS input text.

    Pass an instance of this class via the ``text_filters`` parameter of any
    pipecat TTS service to silently drop emoji before synthesis:

        service = ElevenLabsTTSService(..., text_filters=[EmojiTextFilter()])
    """

    async def filter(self, text: str) -> str:
        return strip_emoji(text)

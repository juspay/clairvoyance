"""Emoji stripping for Breeze Buddy TTS.

Emojis read aloud by a TTS provider come out as noise ("grinning face with
smiling eyes") or an awkward pause — never what we want spoken. This module
removes them from the text handed to the synthesizer WITHOUT touching the text
shown in the UI/transcript: it is wired in two places, both downstream of where
the displayed copy is produced —

  * as a pipecat ``BaseTextFilter`` on every TTS service (``get_tts_service``),
    which the framework applies only to the string sent to the provider — the
    ``AggregatedTextFrame`` pushed downstream for the transcript keeps the
    original text (see pipecat ``TTSService._push_tts_frames``); and
  * directly in the batch greeting synth (``generate_audio``), which calls the
    provider HTTP/WS API and so bypasses the pipecat service.

Covers every voice flow — telephony, Daily, and the widget stream mode
(``TTSSpeakFrame`` goes through the same filter path).

Transcript note: in the widget stream mode the displayed/persisted transcript
is produced independently of TTS (the bridge's RTVI ``transcript`` event and the
chat brain's ``chat_message`` rows), so it keeps its emoji. In the telephony /
Daily full-agent pipeline the assistant context aggregator sits AFTER the TTS
service, so the transcript stored in ``lead.metaData`` reflects the spoken
(emoji-free) text — i.e. it matches the audio. Either way no emoji is voiced.
"""

import re
from typing import Any, Mapping

from pipecat.utils.text.base_text_filter import BaseTextFilter

__all__ = ["strip_emojis", "EmojiTextFilter"]

# Unicode blocks that are entirely emoji / pictographic symbols, plus the
# invisible joiners and modifiers that glue emoji sequences together. None of
# these ranges contain letters, digits, or ordinary punctuation, so removing
# them is safe for ordinary prose.
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001f000-\U0001faff"  # mahjong/dominoes/cards, enclosed alnum/ideographic,
    #                          emoticons, transport, alchemical, geometric-ext,
    #                          arrows-C, supplemental & extended-A pictographs
    "\U00002600-\U000026ff"  # miscellaneous symbols (☀ ☁ ⚡ ♻ …)
    "\U00002700-\U000027bf"  # dingbats (✂ ✅ ✈ ✨ …)
    "\U00002300-\U000023ff"  # misc technical (⌚ ⌛ ⏰ ⏳ ⏪ ⏩ …)
    "\U00002b00-\U00002bff"  # misc symbols & arrows (⭐ ⬆ ⬇ ⬛ …)
    "\U0000fe00-\U0000fe0f"  # variation selectors (emoji vs text presentation)
    "\U0000200d"  # zero-width joiner (emoji sequences)
    "\U000020e3"  # combining enclosing keycap (1️⃣ → 1)
    "]+",
    flags=re.UNICODE,
)

# A handful of emoji are encoded as <base char> + U+FE0F (variation selector-16)
# where the BASE scalar lives outside the blocks above (legacy symbols promoted
# to emoji). Removing FE0F alone would leave the bare glyph to reach TTS, so we
# drop the whole pair — but ONLY when FE0F is present, so legitimate prose like
# "Breeze™" or a literal arrow/square keeps its bare base character.
_VS16_EMOJI_PATTERN = re.compile(
    "["
    "‼⁉™ℹ"  # ‼ ⁉ ™ ℹ
    "↔-↙"  # ↔ ↕ ↖ ↗ ↘ ↙
    "↩↪"  # ↩ ↪
    "Ⓜ"  # Ⓜ
    "▪▫◻-◾"  # ▪ ▫ ◻ ◼ ◽ ◾
    "⤴⤵"  # ⤴ ⤵
    "〰〽"  # 〰 〽
    "]️"  # only when emoji-qualified by VS16 (U+FE0F)
)

# Runs of spaces/tabs left where an emoji sat between words (e.g. "hi 😀 there").
# Newlines are preserved so multi-line prosody is untouched.
_INLINE_WS_RUN = re.compile(r"[ \t]{2,}")


def strip_emojis(text: str) -> str:
    """Remove emoji (and their joiners/modifiers) from ``text`` for TTS.

    Collapses the double space an inter-word emoji leaves behind, but does NOT
    trim leading/trailing whitespace: when streaming tokens the TTS pipeline may
    hand us a single token whose surrounding space separates it from adjacent
    tokens. An all-emoji chunk collapses to empty, which the pipeline's
    post-filter whitespace gate drops without emitting audio.
    """
    if not text:
        return text
    # FE0F-qualified pairs first (atomic), then the dedicated emoji blocks +
    # standalone joiners/modifiers/variation-selectors.
    cleaned = _VS16_EMOJI_PATTERN.sub("", text)
    cleaned = _EMOJI_PATTERN.sub("", cleaned)
    if cleaned == text:
        return text  # fast path — nothing removed, leave the string identical
    return _INLINE_WS_RUN.sub(" ", cleaned)


class EmojiTextFilter(BaseTextFilter):
    """pipecat TTS text filter that drops emoji before synthesis.

    Affects only the text sent to the TTS provider; the transcript/UI copy is
    pushed downstream separately and keeps its emoji.
    """

    async def filter(self, text: str) -> str:
        return strip_emojis(text)

    async def update_settings(self, settings: Mapping[str, Any]) -> None:
        """No configurable settings."""

    async def handle_interruption(self) -> None:
        """Stateless — nothing to reset on interruption."""

    async def reset_interruption(self) -> None:
        """Stateless — nothing to reset after interruption."""

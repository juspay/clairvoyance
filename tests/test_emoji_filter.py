"""Tests for the TTS emoji filter (strip_emojis + EmojiTextFilter)."""

import asyncio

from app.ai.voice.agents.breeze_buddy.tts.emoji_filter import (
    EmojiTextFilter,
    strip_emojis,
)


def test_removes_common_emoji():
    assert strip_emojis("Hello 😀") == "Hello "
    assert strip_emojis("Great job 👍🎉") == "Great job "
    assert strip_emojis("Order shipped 🚚 today") == "Order shipped today"


def test_removes_across_unicode_blocks():
    # emoticons, misc symbols, dingbats, transport, supplemental, technical,
    # misc-symbols-and-arrows, extended-A
    samples = "😀 ☀ ✅ 🚀 🤖 ⌚ ⭐ 🩷"
    assert strip_emojis(samples).strip() == ""


def test_removes_zwj_sequences_and_skin_tones_and_keycaps():
    # ZWJ family, skin-tone modifier, variation selector, keycap
    assert strip_emojis("family 👨‍👩‍👧 here").replace(" ", "") == "familyhere"
    assert strip_emojis("wave 👋🏽").strip() == "wave"
    assert strip_emojis("heart ❤️").strip() == "heart"
    assert strip_emojis("press 1️⃣").strip() == "press 1"  # keycap leaves the digit
    assert strip_emojis("flag 🇮🇳").strip() == "flag"  # regional indicators


def test_collapses_inter_word_double_space_but_keeps_edges():
    # an emoji between words leaves a double space → collapse to one
    assert strip_emojis("hi 😀 there") == "hi there"
    # leading/trailing whitespace is preserved (streaming tokens rely on it)
    assert strip_emojis(" word ") == " word "
    assert strip_emojis("lead 😀") == "lead "


def test_preserves_plain_text_and_non_emoji_symbols():
    # No emoji → identity (fast path), and legit symbols/punctuation untouched
    plain = "Your total is $19.99 (incl. tax) — see Brand™ for details. 100% sure!"
    assert strip_emojis(plain) == plain
    assert strip_emojis("a + b = c, 3 < 4") == "a + b = c, 3 < 4"


def test_removes_vs16_qualified_legacy_symbol_emoji():
    # base char outside the main blocks + U+FE0F → fully removed (not just FE0F)
    assert strip_emojis("note ℹ️ here").replace(" ", "") == "notehere"  # ℹ️
    assert strip_emojis("warning ‼️").strip() == "warning"  # ‼️
    assert strip_emojis("metro Ⓜ️").strip() == "metro"  # Ⓜ️
    assert strip_emojis("see ↔️").strip() == "see"  # ↔️
    # bare base chars (no VS16) are PRESERVED — legit prose / punctuation
    assert strip_emojis("Breeze™ brand") == "Breeze™ brand"  # ™ kept
    assert strip_emojis("range a ↔ b") == "range a ↔ b"  # bare ↔ kept


def test_empty_and_emoji_only():
    assert strip_emojis("") == ""
    assert strip_emojis("😀😀😀") == ""  # all-emoji collapses to empty


def test_filter_is_async_and_strips():
    f = EmojiTextFilter()
    assert asyncio.run(f.filter("Hi 😀 there")) == "Hi there"
    # unchanged text round-trips identically
    assert asyncio.run(f.filter("plain text")) == "plain text"

"""TTS text normalization — expand numbers to words so providers read amounts
correctly (ElevenLabs garbles digit+word hybrids like "5 hundred 99").

Applied in :meth:`CacheService._resolve` after :func:`normalize_text`, to BOTH
the text sent to the provider and the cache key (so "599" and "5 hundred 99"
collapse to one entry).

Scope (gated by ``TTS_NORMALIZE_NUMBERS``):
  * <= 8 integer digits -> Indian cardinal (lakh/crore): 100000 -> "one lakh",
    10000000 -> "one crore"
  * >= 9 integer digits -> digit-by-digit (phone/order ids)
  * decimals -> "point" + up to 10 places: 5.99 -> "five point nine nine"
  * word-multipliers (hundred/thousand/lakh/crore) left as-is

Leading-dot handling (``TTS_LEADING_DOT``, ElevenLabs only — Cartesia reads a
stray "." fine): a "." is PREPENDED to the text sent to ElevenLabs (no space,
idempotent) so it reads the first word/number cleanly. This is a SYNTH-only
hint: it is applied by :func:`prepend_leading_dot` at the provider call site,
NEVER to the cache key, so "your order" and ".your order" share one entry.
"""

from __future__ import annotations

import re

from app.core.config import settings

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = [
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
]

# A number token: digits + commas, optionally a decimal point WITH digits after.
# A trailing "." (sentence period, no following digits) is NOT part of the number
# (else "99." would parse as a decimal -> "ninety nine point ").
_TOKEN_NUM = re.compile(r"^(\W*)(\d[\d,]*(?:\.\d+)?)(\W*)$")


def _under_thousand(x: int) -> str:
    parts = []
    if x >= 100:
        parts.append(f"{_ONES[x // 100]} hundred")
        x %= 100
    if x:
        parts.append(
            _ONES[x]
            if x < 20
            else _TENS[x // 10] + ("" if x % 10 == 0 else " " + _ONES[x % 10])
        )
    return " ".join(parts) or "zero"


def num_to_words(n: int) -> str:
    """Indian cardinal: lakh (10^5), crore (10^7). 100000 -> 'one lakh'."""
    if n == 0:
        return "zero"
    parts = []
    cr, n = divmod(n, 10_000_000)
    la, n = divmod(n, 100_000)
    th, n = divmod(n, 1_000)
    if cr:
        parts.append(_under_thousand(cr) + " crore")
    if la:
        parts.append(_under_thousand(la) + " lakh")
    if th:
        parts.append(_under_thousand(th) + " thousand")
    if n:
        parts.append(_under_thousand(n))
    return " ".join(parts)


def _expand_number(s: str) -> str:
    """Expand a digit string (commas / one decimal allowed) to words. Defensive
    against empty / punctuation-only input (returns it unchanged)."""
    s = s.replace(",", "").strip()
    if not s or not any(c.isdigit() for c in s):
        return s
    if "." in s:  # decimal: integer part as words + "point" + each digit (<=10)
        whole, _, frac = s.partition(".")
        digits = [d for d in frac if d.isdigit()][:10]
        int_part = num_to_words(int(whole)) if whole.isdigit() else "zero"
        if not digits:  # "5." (trailing period) -> just the integer
            return int_part
        return int_part + " point " + " ".join(_ONES[int(d)] for d in digits)
    if len(s) >= 9:  # 9+ integer digits -> digit-by-digit (phone/order id)
        return " ".join(_ONES[int(d)] for d in s if d.isdigit())
    return num_to_words(int(s))


def normalize_numbers(text: str) -> str:
    """Expand standalone digit tokens to English words (Indian grouping).

    Trailing sentence punctuation is kept ("99." -> "ninety nine."). A leading
    "." before a digit stays on the token (".5" -> ".five"); the ElevenLabs
    leading-dot prepend is handled separately by :func:`prepend_leading_dot`
    (synth-only, never the key)."""
    out = []
    for tok in text.split():
        m = _TOKEN_NUM.match(tok)
        if not m:
            out.append(tok)
            continue
        out.append(m.group(1) + _expand_number(m.group(2)) + m.group(3))
    return " ".join(out)


def normalize_for_tts(text: str, provider: str) -> str:
    """Number expansion for the cache KEY and the synth-text base (all providers).

    "599" and "5 hundred 99" collapse to one entry. No-op when
    ``TTS_NORMALIZE_NUMBERS`` is false. This is KEY text — it must be
    provider-hint-free, so the ElevenLabs leading dot is NOT applied here (it is
    a synth-only concern, applied by :func:`prepend_leading_dot`). ``provider``
    is accepted for call-site symmetry but currently does not alter the result.
    """
    if not settings.tts_normalize_numbers:
        return text
    return normalize_numbers(text)


def prepend_leading_dot(text: str, provider: str) -> str:
    """ElevenLabs-only SYNTH hint: ensure ``text`` starts with "." (no space).

    A stray leading dot makes ElevenLabs read the first word/number cleanly (it
    garbles digit+word hybrids like "5 hundred 99" without it). Applied ONLY to
    the text sent to the provider — never to the cache key — so equivalent inputs
    ("your order" and ".your order") share one entry. Other providers are
    unaffected. Idempotent (never double-dots) and a no-op for empty text.

    Independent of number expansion: gated by ``TTS_LEADING_DOT`` alone, so the
    dot still applies when ``TTS_NORMALIZE_NUMBERS`` is off."""
    if (
        settings.tts_leading_dot
        and text
        and text.strip()  # whitespace-only -> no bare "."
        and provider
        and provider.strip().lower() == "elevenlabs"
        and not text.startswith(".")
    ):
        return "." + text
    return text

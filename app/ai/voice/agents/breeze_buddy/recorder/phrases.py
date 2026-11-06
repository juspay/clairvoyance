"""Stop-phrase matching: whole normalised sentence vs a list of phrases."""

import difflib
import unicodedata
from typing import List, Sequence, Union


def normalise(text: str) -> str:
    """Lowercase, strip punctuation and symbols, keep letters/digits/marks."""
    cleaned = "".join(
        c for c in text.strip().lower() if unicodedata.category(c)[0] not in ("P", "S")
    )
    return " ".join(cleaned.split())


def split_phrases(value: Union[str, Sequence[str], None]) -> List[str]:
    """``"a|b"`` or ``["a", "b"]`` → ``["a", "b"]``, blanks dropped."""
    if value is None:
        return []
    parts = value.split("|") if isinstance(value, str) else list(value)
    return [p.strip() for p in parts if p and p.strip()]


def is_stop_command(text: str, phrases: Sequence[str], threshold: float) -> bool:
    """True when the WHOLE sentence matches any phrase (exact or by similarity).

    A longer sentence that merely contains a phrase is not a command.
    """
    sentence = normalise(text)
    if not sentence:
        return False
    for phrase in phrases:
        target = normalise(phrase)
        if not target:
            continue
        if sentence == target:
            return True
        if difflib.SequenceMatcher(None, sentence, target).ratio() >= threshold:
            return True
    return False

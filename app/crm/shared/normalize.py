"""Handle normalization — defined ONCE, imported by identity and platform.

The stored form is the probed form; a format mismatch on a suppressed
value is a compliance bug in the dangerous direction (the gate misses the
suppression and someone who said stop gets contacted). Every writer to a
normalized-key table normalizes through these helpers — no exceptions.
"""

import re
from typing import Optional

_E164 = re.compile(r"^\+[1-9][0-9]{6,14}$")


def normalize_phone(raw: str) -> Optional[str]:
    """Best-effort E.164. Unparseable numbers return None — the handle is
    skipped rather than crashing a contract or violating the CHECK."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        if len(digits) == 10:
            # Bare numbers default to +91 (Indian D2C is the whole market
            # today); a country-code config knob when we leave India.
            digits = "+91" + digits
        elif len(digits) == 11 and digits.startswith("0"):
            digits = "+91" + digits[1:]
        elif len(digits) == 12 and digits.startswith("91"):
            digits = "+" + digits
        else:
            digits = "+" + digits
    return digits if _E164.match(digits) else None


def normalize_email(raw: str) -> Optional[str]:
    """Lowercased, trimmed. Anything without an @ returns None."""
    email = (raw or "").strip().lower()
    return email if "@" in email else None

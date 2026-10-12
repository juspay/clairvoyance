"""What every WhatsApp request body needs, whatever it carries: the
recipient in Meta's form."""

import re
from typing import Optional

_NON_DIGITS = re.compile(r"\D")


def to_meta_recipient(address: str) -> Optional[str]:
    """E.164 in, Meta's digits-only form out.

    Stripping happens HERE and the stripped form is never persisted: one
    representation in the database, whatever each provider prefers at its
    own edge.
    """
    digits = _NON_DIGITS.sub("", address or "")
    # Deliberate parity with shared/normalize.py's ^\+[1-9][0-9]{6,14}$ (and
    # the platform_identity CHECK), so a number this system was willing to
    # store is never rejected here as an "invalid address". 15 is E.164's
    # ceiling; 7 is the real short end (Saint Helena, +290 plus 4 digits);
    # no country code starts with 0.
    if not 7 <= len(digits) <= 15 or digits.startswith("0"):
        return None
    return digits

"""How a customer's contact details may appear in a log — defined ONCE.

The mirror of normalize.py: that file fixes the form a handle is STORED in,
this one the form it may be WRITTEN OUT in. One module logging a full phone
number undoes the discipline of every module that doesn't.

Imported directly (shared/ is exempt from the contracts-only rule).
"""

import re

FULL_MASK = "****"

# 7 digits = E.164's floor, so shorter runs (error codes, dates, counts)
# survive — a log stripped of every number stops being useful.
_LONG_DIGIT_RUN = re.compile(r"\d{7,}")


def _mask_phone(phone: str) -> str:
    """Last four digits of the national number, everything else starred.

    Self-contained on purpose: the previous import came from a legacy
    queries file, which is not a home this module may depend on. The
    rendering matches the voice path's mask_phone exactly (digits only,
    last 10 kept, ≤4 digits fully masked) so one number reads the same
    in every log.
    """
    digits = re.sub(r"\D", "", phone or "")[-10:]
    if len(digits) <= 4:
        return FULL_MASK
    return "*" * (len(digits) - 4) + digits[-4:]


def mask_digit_runs(text: str) -> str:
    """Mask any digit run long enough to be a contact number.

    For text WE did not write — a provider's error message may echo back the
    value it rejected ("Invalid parameter: to=9198…"). mask_address covers
    what we log on purpose; this covers what somebody else smuggles in.
    """
    return _LONG_DIGIT_RUN.sub(FULL_MASK, text or "")


def mask_address(address: str, channel: str) -> str:
    """``address`` as it may appear in a log, masked by ``channel``'s rule.

    The channel picks the rule, never the value's shape: sniffing would make
    the mask depend on the data being well-formed, which is exactly when
    masking matters most. A channel with no branch masks EVERYTHING — new
    channels opt in to revealing anything. Total: empty input masks rather
    than raising, because a log line is never worth an exception.
    """
    if channel == "whatsapp":
        return _mask_phone(address or "")
    return FULL_MASK

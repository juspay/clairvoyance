"""Canonical phone-number parsing for outbound dialing.

Callers must either supply an international number (starting with ``+``) or
an explicit ISO 3166-1 alpha-2 region. There is deliberately no implicit
default region: the generic lead API cannot safely infer a country from a
local number.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from phonenumbers import (
    PhoneNumberFormat,
    format_number,
    is_possible_number,
    is_valid_number,
    parse,
)
from phonenumbers.phonenumberutil import NumberParseException


class InvalidPhoneNumber(ValueError):
    """Raised when a value cannot be used as an outbound dialing number."""


class PhoneNumberDisposition(str, Enum):
    """Policy outcome for a phone number at a specific boundary."""

    DIALABLE = "dialable"
    REJECT = "reject"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class DialingPhoneNumber:
    """A validated outbound phone number in canonical E.164 form."""

    e164: str

    @classmethod
    def parse(cls, raw: object, *, region: str | None = None) -> "DialingPhoneNumber":
        """Parse and validate ``raw`` for outbound dialing.

        Without ``region``, ``raw`` must be internationally qualified with a
        leading ``+``. Integrations that know their market may explicitly pass
        a region, for example ``region="IN"`` for an Indian checkout.
        """
        if not isinstance(raw, str):
            raise InvalidPhoneNumber("phone number must be a string")

        candidate = raw.strip()
        if not candidate:
            raise InvalidPhoneNumber("phone number is required")
        if region is None and not candidate.startswith("+"):
            raise InvalidPhoneNumber(
                "phone number must start with '+' and include a country code"
            )

        try:
            parsed = parse(candidate, region)
        except NumberParseException as exc:
            raise InvalidPhoneNumber("phone number could not be parsed") from exc

        if not is_possible_number(parsed) or not is_valid_number(parsed):
            raise InvalidPhoneNumber("phone number is not valid")

        return cls(format_number(parsed, PhoneNumberFormat.E164))

    def __str__(self) -> str:
        return self.e164


@dataclass(frozen=True, slots=True)
class PhoneNumberNormalization:
    """Structured phone normalization result for boundary-specific policy."""

    disposition: PhoneNumberDisposition
    e164: str | None = None
    reason: str | None = None

    @property
    def is_dialable(self) -> bool:
        return (
            self.disposition is PhoneNumberDisposition.DIALABLE
            and self.e164 is not None
        )


def canonicalize_phone_number(raw: object, *, region: str | None = None) -> str:
    """Return a validated E.164 dialing number."""
    return DialingPhoneNumber.parse(raw, region=region).e164


def normalize_phone_number(
    raw: object, *, region: str | None = None
) -> PhoneNumberNormalization:
    """Return a structured dialing decision for a generic lead phone number."""
    try:
        return PhoneNumberNormalization(
            disposition=PhoneNumberDisposition.DIALABLE,
            e164=canonicalize_phone_number(raw, region=region),
        )
    except InvalidPhoneNumber as exc:
        return PhoneNumberNormalization(
            disposition=PhoneNumberDisposition.REJECT,
            reason=str(exc),
        )


def normalize_optional_phone_number(
    raw: object, *, region: str | None = None
) -> PhoneNumberNormalization:
    """Return SKIP for missing optional phones, otherwise validate."""
    if raw is None or raw == "":
        return PhoneNumberNormalization(
            disposition=PhoneNumberDisposition.SKIP,
            reason="phone number is missing",
        )
    return normalize_phone_number(raw, region=region)


def normalize_indian_phone_for_dialing(raw: object) -> str:
    """Return an E.164 Indian-market dialing number, or ``""`` to skip."""
    result = normalize_optional_phone_number(raw, region="IN")
    if result.is_dialable and result.e164:
        return result.e164
    return ""

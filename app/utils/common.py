"""
Common utilities for validation, parsing, and helper functions.
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config.static import AWS_BREEZE_PORTAL_URL, GCP_BREEZE_PORTAL_URL
from app.core.logger import logger


def utcnow() -> datetime:
    """Tz-aware UTC now — use for any column declared ``timestamptz``."""
    return datetime.now(timezone.utc)


def parse_iso_datetime(iso_string: Optional[str]) -> Optional[datetime]:
    """
    Parse ISO 8601 datetime string to datetime object.
    Handles various ISO format variations including:
    - 2023-12-25T10:30:00Z
    - 2023-12-25T10:30:00+00:00
    - 2023-12-25T10:30:00.123Z
    - 2023-12-25T10:30:00.123456+05:30

    Args:
        iso_string: ISO 8601 formatted datetime string

    Returns:
        datetime object or None if parsing fails
    """
    if not iso_string:
        return None

    try:
        # Handle 'Z' suffix by replacing with '+00:00'
        if iso_string.endswith("Z"):
            iso_string = iso_string[:-1] + "+00:00"

        # Use fromisoformat which handles most ISO 8601 variations
        return datetime.fromisoformat(iso_string)
    except ValueError as e:
        logger.error(f"Failed to parse datetime string '{iso_string}': {e}")
        # Fallback: try to parse without timezone info
        try:
            # Remove timezone info and parse as naive datetime
            if "+" in iso_string:
                iso_string = iso_string.split("+")[0]
            elif iso_string.count("-") > 2:  # Has timezone with minus
                # Find the last occurrence of '-' which should be timezone
                parts = iso_string.rsplit("-", 1)
                if len(parts) == 2 and ":" in parts[1]:
                    iso_string = parts[0]

            return datetime.fromisoformat(iso_string)
        except ValueError:
            logger.error(
                f"Failed to parse datetime string even without timezone: '{iso_string}'"
            )
            return None


def parse_json(row, key) -> Optional[Dict[str, Any]]:
    return (
        row[key]
        if isinstance(row[key], dict)
        else json.loads(row[key]) if row[key] else None
    )


def get_breeze_portal_url(reseller_id: str | None = None) -> str:
    """
    Get the appropriate Breeze portal base URL based on reseller ID.

    Args:
        reseller_id: The reseller identifier. If "super_reseller", returns the SDK store URL.
                    Otherwise, returns the standard portal URL.

    Returns:
        str: The base URL for the Breeze portal
    """
    if reseller_id == "super_reseller":
        return GCP_BREEZE_PORTAL_URL
    else:
        return AWS_BREEZE_PORTAL_URL


def parse_json_field(value) -> List[str]:
    """Parse JSON field from database, handling both string and list types.

    Args:
        value: The value to parse (can be str, list, or None)

    Returns:
        List of strings, empty list if parsing fails or value is None

    Examples:
        >>> parse_json_field('["a", "b", "c"]')
        ['a', 'b', 'c']
        >>> parse_json_field(["a", "b"])
        ['a', 'b']
        >>> parse_json_field(None)
        []
        >>> parse_json_field("invalid json")
        []
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON in database field: {value}")
            return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


_E164 = re.compile(r"^\+[1-9][0-9]{6,14}$")


def normalize_e164(raw: object) -> Optional[str]:
    """Best-effort E.164. Returns None when the value cannot be dialed."""
    if not isinstance(raw, str):
        return None
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        if len(digits) == 10:
            # Bare numbers default to +91, matching the crm normalizer.
            digits = "+91" + digits
        elif len(digits) == 11 and digits.startswith("0"):
            digits = "+91" + digits[1:]
        elif len(digits) == 12 and digits.startswith("91"):
            digits = "+" + digits
        else:
            digits = "+" + digits
    return digits if _E164.match(digits) else None


def is_dialable(raw: object) -> bool:
    """True when ``raw`` normalizes to a valid E.164 number."""
    return normalize_e164(raw) is not None

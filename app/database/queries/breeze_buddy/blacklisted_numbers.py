"""
Database query functions for blacklisted numbers.
"""

import re
from datetime import datetime
from typing import Any, List, Optional, Tuple

BLACKLISTED_NUMBERS_TABLE = "blacklisted_numbers"


def normalize_phone_number(phone_number: str) -> str:
    """
    Normalize a phone number by stripping non-digit characters
    and keeping only the last 10 digits (for Indian numbers).

    Returns empty string for inputs with no digits.
    """
    if not phone_number:
        return ""
    digits = re.sub(r"\D", "", phone_number)
    if not digits:
        return ""
    return digits[-10:] if len(digits) >= 10 else digits


def mask_phone(phone_number: str) -> str:
    """Mask phone number for logging, showing only last 4 digits."""
    normalized = normalize_phone_number(phone_number)
    if len(normalized) <= 4:
        return "****"
    return "*" * (len(normalized) - 4) + normalized[-4:]


def insert_blacklisted_number_query(
    id: str,
    phone_number: str,
    merchant_id: Optional[str] = None,
    reason: Optional[str] = None,
    created_by: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to insert a blacklisted number.
    """
    text = f"""
        INSERT INTO "{BLACKLISTED_NUMBERS_TABLE}"
        ("id", "phone_number", "merchant_id", "reason", "created_by", "created_at", "updated_at")
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING *;
    """
    values = [
        id,
        normalize_phone_number(phone_number),
        merchant_id,
        reason,
        created_by,
        datetime.now(),
        datetime.now(),
    ]
    return text, values


def is_number_blacklisted_query(
    phone_number: str,
    merchant_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to check if a phone number is blacklisted.
    Checks both global blacklist (merchant_id IS NULL) and per-merchant blacklist.
    """
    normalized = normalize_phone_number(phone_number)

    if merchant_id:
        text = f"""
            SELECT EXISTS(
                SELECT 1 FROM "{BLACKLISTED_NUMBERS_TABLE}"
                WHERE "phone_number" = $1
                AND ("merchant_id" = $2 OR "merchant_id" IS NULL)
            ) AS is_blacklisted;
        """
        values: List[Any] = [normalized, merchant_id]
    else:
        text = f"""
            SELECT EXISTS(
                SELECT 1 FROM "{BLACKLISTED_NUMBERS_TABLE}"
                WHERE "phone_number" = $1
            ) AS is_blacklisted;
        """
        values = [normalized]

    return text, values


def delete_blacklisted_number_query(
    phone_number: str,
    merchant_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to delete a blacklisted number.
    """
    normalized = normalize_phone_number(phone_number)

    if merchant_id:
        text = f"""
            DELETE FROM "{BLACKLISTED_NUMBERS_TABLE}"
            WHERE "phone_number" = $1 AND "merchant_id" = $2
            RETURNING *;
        """
        values: List[Any] = [normalized, merchant_id]
    else:
        text = f"""
            DELETE FROM "{BLACKLISTED_NUMBERS_TABLE}"
            WHERE "phone_number" = $1 AND "merchant_id" IS NULL
            RETURNING *;
        """
        values = [normalized]

    return text, values


def get_all_blacklisted_numbers_query(
    merchant_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get all blacklisted numbers with optional merchant filter.
    """
    if merchant_id:
        text = f"""
            SELECT * FROM "{BLACKLISTED_NUMBERS_TABLE}"
            WHERE "merchant_id" = $1 OR "merchant_id" IS NULL
            ORDER BY "created_at" DESC;
        """
        values: List[Any] = [merchant_id]
    else:
        text = f"""
            SELECT * FROM "{BLACKLISTED_NUMBERS_TABLE}"
            ORDER BY "created_at" DESC;
        """
        values = []

    return text, values


def get_blacklisted_number_by_phone_query(
    phone_number: str,
) -> Tuple[str, List[Any]]:
    """
    Generate query to check blacklist entries for a specific phone number.
    """
    normalized = normalize_phone_number(phone_number)
    text = f"""
        SELECT * FROM "{BLACKLISTED_NUMBERS_TABLE}"
        WHERE "phone_number" = $1;
    """
    values = [normalized]
    return text, values

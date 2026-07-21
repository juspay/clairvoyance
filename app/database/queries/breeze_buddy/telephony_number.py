"""
Database query functions for the application.
"""

from datetime import datetime
from typing import Any, List, Optional, Tuple

from app.database.queries.breeze_buddy.lead_call_tracker import LEAD_CALL_TRACKER_TABLE
from app.schemas import CallProvider, TelephonyNumberStatus

# Table names
TELEPHONY_NUMBER_TABLE = "telephony_numbers"


# Outbound number queries
def insert_telephony_number_query(
    id: str,
    number: str,
    provider: CallProvider,
    status: TelephonyNumberStatus,
    channels: Optional[int] = None,
    maximum_channels: Optional[int] = None,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to insert outbound number record.
    """
    text = f"""
        INSERT INTO "{TELEPHONY_NUMBER_TABLE}"
        (
            "id",
            "number",
            "provider",
            "status",
            "channels",
            "maximum_channels",
            "reseller_id",
            "merchant_id",
            "created_at",
            "updated_at"
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING *;
    """

    values = [
        id,
        number,
        provider.value,
        status.value,
        channels,
        maximum_channels,
        reseller_id,
        merchant_id,
        datetime.now(),
        datetime.now(),
    ]

    return text, values


def get_telephony_number_by_id_query(outbound_number_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get outbound number by ID.
    """
    text = f"""
        SELECT *
        FROM "{TELEPHONY_NUMBER_TABLE}"
        WHERE "id" = $1;
    """
    values = [outbound_number_id]
    return text, values


def update_telephony_number_status_query(
    outbound_number_id: str, status: TelephonyNumberStatus
) -> Tuple[str, List[Any]]:
    """
    Generate query to update outbound number status.
    """
    text = f"""
        UPDATE "{TELEPHONY_NUMBER_TABLE}"
        SET "status" = $2, "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    values = [outbound_number_id, status.value]
    return text, values


def increment_telephony_number_channels_query(
    outbound_number_id: str,
) -> Tuple[str, List[Any]]:
    """
    Generate query to atomically increment outbound number channels by 1.
    Only increments if channels < maximum_channels to enforce the capacity limit.
    Returns no rows if the number is at capacity, allowing the caller to detect this.
    This avoids race conditions by using database-level atomic increment with constraint.
    """
    text = f"""
        UPDATE "{TELEPHONY_NUMBER_TABLE}"
        SET "channels" = COALESCE("channels", 0) + 1, "updated_at" = NOW()
        WHERE "id" = $1
          AND COALESCE("channels", 0) < COALESCE("maximum_channels", 0)
        RETURNING *;
    """
    values = [outbound_number_id]
    return text, values


def decrement_telephony_number_channels_query(
    outbound_number_id: str,
) -> Tuple[str, List[Any]]:
    """
    Generate query to atomically decrement outbound number channels by 1.
    Uses GREATEST to ensure channels never goes below 0.
    This avoids race conditions by using database-level atomic decrement.
    """
    text = f"""
        UPDATE "{TELEPHONY_NUMBER_TABLE}"
        SET "channels" = GREATEST(0, COALESCE("channels", 0) - 1), "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    values = [outbound_number_id]
    return text, values


def disable_telephony_number_query(outbound_number_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to disable outbound number by ID.
    """
    text = f"""
        UPDATE "{TELEPHONY_NUMBER_TABLE}"
        SET "status" = $2, "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    values = [outbound_number_id, TelephonyNumberStatus.DISABLED.value]
    return text, values


def get_all_telephony_numbers_query() -> Tuple[str, List[Any]]:
    """
    Generate query to get all outbound numbers.
    """
    text = f"""
        SELECT * FROM "{TELEPHONY_NUMBER_TABLE}"
        ORDER BY "created_at" DESC;
    """
    values = []
    return text, values


def get_all_telephony_numbers_with_call_count_query(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get all outbound numbers with their call counts and no answer breakdown.
    """

    values: List[Any] = []
    conditions = []

    if start_date:
        values.append(start_date)
        conditions.append(f'lct."call_initiated_time" >= ${len(values)}')

    if end_date:
        values.append(end_date)
        conditions.append(f'lct."call_initiated_time" < ${len(values)}')

    where_clause = ""
    if conditions:
        where_clause = " AND " + " AND ".join(conditions)

    text = f"""
        SELECT 
            ou.*,
            COALESCE(COUNT(lct.id), 0) AS total_calls,
            COALESCE(COUNT(lct.id) FILTER (WHERE lct.outcome = 'NO_ANSWER'), 0) AS calls_no_answer
        FROM "{TELEPHONY_NUMBER_TABLE}" ou
        LEFT JOIN "{LEAD_CALL_TRACKER_TABLE}" lct ON ou.id = lct.outbound_number_id{where_clause}
        GROUP BY ou.id
        ORDER BY ou.created_at DESC;
    """
    return text, values


def get_telephony_number_based_on_status_and_provider_query(
    status: TelephonyNumberStatus, provider: CallProvider
) -> Tuple[str, List[Any]]:
    """
    Generate query to get outbound number by status and provider.
    """
    text = f"""
        SELECT *
        FROM "{TELEPHONY_NUMBER_TABLE}"
        WHERE "status" = $1 AND "provider" = $2
        ORDER BY "created_at" DESC;
    """
    values = [status.value, provider.value]
    return text, values


def get_telephony_number_by_number_query(number: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get outbound number by phone number.
    """
    text = f'SELECT * FROM "{TELEPHONY_NUMBER_TABLE}" WHERE "number" = $1;'
    values = [number]
    return text, values


def update_telephony_number_query(
    telephony_number_id: str,
    status: Optional[TelephonyNumberStatus] = None,
    maximum_channels: Optional[int] = None,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    clear_ownership: bool = False,
    set_ownership: bool = False,
) -> Tuple[str, List[Any]]:
    """
    Generate a partial-update query for a telephony number.

    Ownership is all-or-nothing: set_ownership=True writes BOTH reseller_id
    and merchant_id from the given values (None → NULL), so a reassignment
    can never leave a stale half of the previous owner behind (e.g. moving a
    merchant-owned number to umbrella-only must null merchant_id).
    clear_ownership=True nulls both (returns the number to the shared
    platform pool) and wins over set_ownership. With neither flag,
    reseller_id/merchant_id are ignored and ownership is left unchanged.
    """
    sets: List[str] = []
    values: List[Any] = [telephony_number_id]

    def add(col: str, val: Any) -> None:
        values.append(val)
        sets.append(f'"{col}" = ${len(values)}')

    if status is not None:
        add("status", status.value)
    if maximum_channels is not None:
        add("maximum_channels", maximum_channels)
    if clear_ownership:
        sets.append('"reseller_id" = NULL')
        sets.append('"merchant_id" = NULL')
    elif set_ownership:
        add("reseller_id", reseller_id)
        add("merchant_id", merchant_id)
    sets.append('"updated_at" = NOW()')

    text = f"""
        UPDATE "{TELEPHONY_NUMBER_TABLE}"
        SET {", ".join(sets)}
        WHERE "id" = $1
        RETURNING *;
    """
    return text, values


def get_template_pinned_number_ids_query(
    merchant_ids: List[str], reseller_ids: List[str]
) -> Tuple[str, List[Any]]:
    """
    Generate query for the ids of numbers pinned (template.outbound_number_id)
    by templates belonging to any of the given merchants or umbrellas. Used by
    the numbers RBAC filter so non-admins can see the shared-pool numbers their
    agents actually dial from without seeing the whole fleet.
    """
    text = """
        SELECT DISTINCT "outbound_number_id"
        FROM "template"
        WHERE "outbound_number_id" IS NOT NULL
          AND ("merchant_id" = ANY($1) OR "reseller_id" = ANY($2));
    """
    values: List[Any] = [list(merchant_ids), list(reseller_ids)]
    return text, values

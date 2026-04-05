"""
Database query functions for the application.
"""

from datetime import datetime
from typing import Any, List, Optional, Tuple

from app.database.queries.breeze_buddy.lead_call_tracker import LEAD_CALL_TRACKER_TABLE
from app.schemas import CallProvider, OutboundNumberStatus

# Table names
OUTBOUND_NUMBER_TABLE = "outbound_number"


# Outbound number queries
def insert_outbound_number_query(
    id: str,
    number: str,
    provider: CallProvider,
    status: OutboundNumberStatus,
    channels: Optional[int] = None,
    maximum_channels: Optional[int] = None,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to insert outbound number record.
    """
    text = f"""
        INSERT INTO "{OUTBOUND_NUMBER_TABLE}"
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


def get_outbound_number_by_id_query(outbound_number_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get outbound number by ID.
    """
    text = f"""
        SELECT *
        FROM "{OUTBOUND_NUMBER_TABLE}"
        WHERE "id" = $1;
    """
    values = [outbound_number_id]
    return text, values


def update_outbound_number_status_query(
    outbound_number_id: str, status: OutboundNumberStatus
) -> Tuple[str, List[Any]]:
    """
    Generate query to update outbound number status.
    """
    text = f"""
        UPDATE "{OUTBOUND_NUMBER_TABLE}"
        SET "status" = $2, "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    values = [outbound_number_id, status.value]
    return text, values


def increment_outbound_number_channels_query(
    outbound_number_id: str,
) -> Tuple[str, List[Any]]:
    """
    Generate query to atomically increment outbound number channels by 1.
    Only increments if channels < maximum_channels to enforce the capacity limit.
    Returns no rows if the number is at capacity, allowing the caller to detect this.
    This avoids race conditions by using database-level atomic increment with constraint.
    """
    text = f"""
        UPDATE "{OUTBOUND_NUMBER_TABLE}"
        SET "channels" = COALESCE("channels", 0) + 1, "updated_at" = NOW()
        WHERE "id" = $1
          AND COALESCE("channels", 0) < COALESCE("maximum_channels", 0)
        RETURNING *;
    """
    values = [outbound_number_id]
    return text, values


def decrement_outbound_number_channels_query(
    outbound_number_id: str,
) -> Tuple[str, List[Any]]:
    """
    Generate query to atomically decrement outbound number channels by 1.
    Uses GREATEST to ensure channels never goes below 0.
    This avoids race conditions by using database-level atomic decrement.
    """
    text = f"""
        UPDATE "{OUTBOUND_NUMBER_TABLE}"
        SET "channels" = GREATEST(0, COALESCE("channels", 0) - 1), "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    values = [outbound_number_id]
    return text, values


def disable_outbound_number_query(outbound_number_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to disable outbound number by ID.
    """
    text = f"""
        UPDATE "{OUTBOUND_NUMBER_TABLE}"
        SET "status" = $2, "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    values = [outbound_number_id, OutboundNumberStatus.DISABLED.value]
    return text, values


def get_all_outbound_numbers_query() -> Tuple[str, List[Any]]:
    """
    Generate query to get all outbound numbers.
    """
    text = f"""
        SELECT * FROM "{OUTBOUND_NUMBER_TABLE}"
        ORDER BY "created_at" DESC;
    """
    values = []
    return text, values


def get_all_outbound_numbers_with_call_count_query(
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
        FROM "{OUTBOUND_NUMBER_TABLE}" ou
        LEFT JOIN "{LEAD_CALL_TRACKER_TABLE}" lct ON ou.id = lct.outbound_number_id{where_clause}
        GROUP BY ou.id
        ORDER BY ou.created_at DESC;
    """
    return text, values


def get_outbound_number_based_on_status_and_provider_query(
    status: OutboundNumberStatus, provider: CallProvider
) -> Tuple[str, List[Any]]:
    """
    Generate query to get outbound number by status and provider.
    """
    text = f"""
        SELECT *
        FROM "{OUTBOUND_NUMBER_TABLE}"
        WHERE "status" = $1 AND "provider" = $2
        ORDER BY "created_at" DESC;
    """
    values = [status.value, provider.value]
    return text, values


def get_outbound_number_by_number_query(number: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get outbound number by phone number.
    """
    text = f'SELECT * FROM "{OUTBOUND_NUMBER_TABLE}" WHERE "number" = $1;'
    values = [number]
    return text, values

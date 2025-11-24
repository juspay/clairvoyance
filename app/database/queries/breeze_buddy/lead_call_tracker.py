"""
Database query functions for the application.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.schemas import LeadCallOutcome, LeadCallStatus, Workflow

# Table names
LEAD_CALL_TRACKER_TABLE = "lead_call_tracker"
OUTBOUND_NUMBER_TABLE = "outbound_number"


# Lead call tracker queries
def insert_lead_call_tracker_query(
    id: str,
    merchant_id: str,
    workflow: Workflow,
    shop_identifier: Optional[str],
    next_attempt_at: Optional[datetime],
    payload: Optional[Dict[str, Any]],
    meta_data: Optional[Dict[str, Any]],
    call_end_time: Optional[datetime] = None,
    attempt_count: int = 0,
    call_initiated_time: Optional[datetime] = None,
    cost: Optional[float] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to insert lead call tracker record.
    """
    text = f"""
        INSERT INTO "{LEAD_CALL_TRACKER_TABLE}"
        (
            "id",
            "merchant_id",
            "workflow",
            "shop_identifier",
            "next_attempt_at",
            "payload",
            "meta_data",
            "status",
            "call_initiated_time",
            "call_end_time",
            "attempt_count",
            "cost",
            "created_at",
            "updated_at"
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) RETURNING *;
    """

    values = [
        id,
        merchant_id,
        workflow.value,
        shop_identifier,
        next_attempt_at,
        json.dumps(payload) if payload else None,
        json.dumps(meta_data) if meta_data else None,
        LeadCallStatus.BACKLOG.value,
        call_initiated_time,
        call_end_time,
        attempt_count,
        cost,
        datetime.now(),
        datetime.now(),
    ]

    return text, values


def get_leads_based_on_status_and_next_attempt_query(
    status: LeadCallStatus, time: datetime
) -> Tuple[str, List[Any]]:
    """
    Generate query to select leads based on status and next attempt time.
    """
    text = f"""
        SELECT * FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "status" = $1
        AND "next_attempt_at" <= $2
        AND "is_locked" = FALSE;
    """
    values = [status.value, time]
    return text, values


def acquire_lock_on_lead_by_id_query(lead_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to atomically acquire lock on a lead by ID.
    Returns the lead if successfully locked, None if already locked.
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "is_locked" = TRUE, "updated_at" = NOW()
        WHERE "id" = $1
        AND "is_locked" = FALSE
        RETURNING *;
    """
    values = [lead_id]
    return text, values


def release_lock_on_lead_by_id_query(lead_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to release lock on a lead by ID.
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "is_locked" = FALSE, "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    values = [lead_id]
    return text, values


def update_lead_call_details_query(
    id: str,
    status: LeadCallStatus,
    call_id: str,
    call_initiated_time: datetime,
    outbound_number_id: str,
) -> Tuple[str, List[Any]]:
    """
    Generate query to update lead call details.
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "status" = $1, "call_id" = $2, "updated_at" = NOW(), "call_initiated_time" = $3, "outbound_number_id" = $4
        WHERE "id" = $5
        RETURNING *;
    """
    values = [status.value, call_id, call_initiated_time, outbound_number_id, id]
    return text, values


def get_lead_by_call_id_query(call_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get lead by call ID.
    """
    text = f"""
        SELECT * FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "call_id" = $1;
    """
    values = [call_id]
    return text, values


def get_lead_by_id_query(lead_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get lead by ID.
    """
    text = f"""
        SELECT * FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "id" = $1;
    """
    values = [lead_id]
    return text, values


def update_lead_call_recording_url_query(
    call_id: str, recording_url: str
) -> Tuple[str, List[Any]]:
    """
    Generate query to update lead call recording url.
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "recording_url" = $1, "updated_at" = NOW()
        WHERE "call_id" = $2
        RETURNING *;
    """
    values = [recording_url, call_id]
    return text, values


def update_lead_call_initiated_time_query(
    call_id: str, call_initiated_time: datetime
) -> Tuple[str, List[Any]]:
    """
    Generate query to update lead call initiated time.
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "call_initiated_time" = $1, "updated_at" = NOW()
        WHERE "call_id" = $2
        RETURNING *;
    """
    values = [call_initiated_time, call_id]
    return text, values


def update_lead_call_completion_details_query(
    id: str,
    status: LeadCallStatus,
    outcome: LeadCallOutcome,
    meta_data: Dict[str, Any],
    call_end_time: datetime,
) -> Tuple[str, List[Any]]:
    """
    Generate query to update lead call completion details.
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "status" = $1, "outcome" = $2, "meta_data" = $3, "call_end_time" = $4, "updated_at" = NOW()
        WHERE "id" = $5
        RETURNING *;
    """
    values = [status.value, outcome.value, json.dumps(meta_data), call_end_time, id]
    return text, values


def get_all_lead_call_trackers_query(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    outcome: Optional[str] = None,
    order_id: Optional[str] = None,
    shop_name: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get all lead call trackers within a date range with optional filters and pagination.
    """
    text = f"""
        SELECT
            lct.*,
            ou.provider as calling_provider
        FROM
            "{LEAD_CALL_TRACKER_TABLE}" lct
        LEFT JOIN
            "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id
    """
    values: List[Any] = []
    conditions = []

    if start_date:
        values.append(start_date)
        conditions.append(f'lct."call_initiated_time" >= ${len(values)}')

    if end_date:
        values.append(end_date)
        conditions.append(f'lct."call_initiated_time" < ${len(values)}')

    if outcome:
        values.append(outcome)
        conditions.append(f"outcome = ${len(values)}")

    if order_id:
        values.append(f"%{order_id}%")
        conditions.append(f"payload->>'order_id' LIKE ${len(values)}")

    if shop_name:
        values.append(f"%{shop_name}%")
        conditions.append(f"payload->>'shop_name' LIKE ${len(values)}")

    if conditions:
        text += " WHERE " + " AND ".join(conditions)

    text += ' ORDER BY lct."created_at" DESC'

    if limit is not None:
        values.append(limit)
        text += f" LIMIT ${len(values)}"

    if offset is not None:
        values.append(offset)
        text += f" OFFSET ${len(values)}"

    text += ";"
    return text, values


def get_leads_by_status_and_time_before_query(
    status: LeadCallStatus, time: datetime
) -> Tuple[str, List[Any]]:
    """
    Generate query to select leads based on their status and a time before which they were initiated.
    Only returns unlocked leads to prevent race conditions.
    """
    text = f"""
        SELECT * FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "status" = $1
        AND "call_initiated_time" < $2
        AND "is_locked" = FALSE;
    """
    values = [status.value, time]
    return text, values


def get_lead_call_trackers_count_query(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    outcome: Optional[str] = None,
    order_id: Optional[str] = None,
    shop_name: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to count all lead call trackers within a date range with optional filters.
    """
    text = f"""
        SELECT COUNT(*) FROM "{LEAD_CALL_TRACKER_TABLE}"
    """
    values: List[Any] = []
    conditions = []

    if start_date:
        values.append(start_date)
        conditions.append(f'"{LEAD_CALL_TRACKER_TABLE}"."created_at" >= ${len(values)}')

    if end_date:
        values.append(end_date)
        conditions.append(f'"{LEAD_CALL_TRACKER_TABLE}"."created_at" < ${len(values)}')

    if outcome:
        values.append(outcome)
        conditions.append(f"outcome = ${len(values)}")

    if order_id:
        values.append(f"%{order_id}%")
        conditions.append(f"payload->>'order_id' LIKE ${len(values)}")

    if shop_name:
        values.append(f"%{shop_name}%")
        conditions.append(f"payload->>'shop_name' LIKE ${len(values)}")

    if conditions:
        text += " WHERE " + " AND ".join(conditions)

    text += ";"
    return text, values


def get_lead_based_analytics_query(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get per-lead call data.
    Returns one row per order_id with call counts. Aggregation done in Python.
    """
    values: List[Any] = []
    conditions = []

    if start_date:
        values.append(start_date)
        conditions.append(f'"call_initiated_time" >= ${len(values)}')

    if end_date:
        values.append(end_date)
        conditions.append(f'"call_initiated_time" < ${len(values)}')

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    text = f"""
        SELECT
            payload ->> 'order_id' AS order_id,
            COUNT(*) AS total_calls,
            COUNT(*) FILTER (WHERE status = 'FINISHED') AS finished_calls,
            COUNT(*) FILTER (WHERE outcome = 'CONFIRM') AS confirmed_calls,
            COUNT(*) FILTER (WHERE outcome = 'CANCEL') AS cancelled_calls,
            COUNT(*) FILTER (WHERE outcome = 'ADDRESS_UPDATED') AS address_update_calls,
            COUNT(*) FILTER (WHERE outcome = 'BUSY') AS busy_calls,
            COUNT(*) FILTER (WHERE outcome = 'NO_ANSWER') AS no_answer_calls
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        {where_clause}
        GROUP BY payload ->> 'order_id';
    """
    return text, values

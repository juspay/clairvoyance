"""
Database query functions for the application.
"""
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from app.schemas import Workflow, LeadCallStatus, LeadCallOutcome, RequestedBy
import json

# Table names
LEAD_CALL_TRACKER_TABLE = "lead_call_tracker"

# Lead call tracker queries
def insert_lead_call_tracker_query(
    id: str,
    merchant_id: RequestedBy,
    workflow: Workflow,
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
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING *;
    """

    values = [
        id,
        merchant_id.value,
        workflow.value,
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
        AND "next_attempt_at" <= $2;
    """
    values = [status.value, time]
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

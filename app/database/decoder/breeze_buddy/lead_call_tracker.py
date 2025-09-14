"""
Decoder functions for lead call tracker.
"""
from typing import List, Optional
import asyncpg
from app.schemas import (
    LeadCallTracker,
    Workflow,
    LeadCallStatus,
    LeadCallOutcome,
    RequestedBy,
)
from app.utils.common import parse_json


def decode_lead_call_tracker(row: asyncpg.Record) -> Optional[LeadCallTracker]:
    """
    Decode lead call tracker from database result using Pydantic model.
    """
    if not row:
        return None

    return LeadCallTracker(
        id=row["id"],
        outbound_number_id=row["outbound_number_id"],
        merchant_id=RequestedBy(row["merchant_id"]),
        workflow=Workflow(row["workflow"]),
        attempt_count=row["attempt_count"],
        next_attempt_at=row["next_attempt_at"],
        payload=parse_json(row, "payload"),
        metaData=parse_json(row, "meta_data"),
        recording_url=row["recording_url"],
        status=LeadCallStatus(row["status"]),
        outcome=LeadCallOutcome(row["outcome"]) if row["outcome"] else None,
        call_id=row["call_id"],
        call_initiated_time=row["call_initiated_time"],
        call_end_time=row["call_end_time"],
        cost=row["cost"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

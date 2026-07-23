"""
Decoder functions for lead call tracker.
"""

from typing import Optional

import asyncpg

from app.schemas import (
    CallDirection,
    ExecutionMode,
    LeadCallStatus,
    LeadCallTracker,
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
        telephony_number_id=row["telephony_number_id"],
        reseller_id=row["reseller_id"],
        template=row["template"],
        template_id=str(row["template_id"]) if row.get("template_id") else None,
        merchant_id=row["merchant_id"],
        request_id=row.get("request_id"),
        attempt_count=row["attempt_count"],
        next_attempt_at=row["next_attempt_at"],
        payload=parse_json(row, "payload"),
        metaData=parse_json(row, "meta_data"),
        recording_url=row["recording_url"],
        status=LeadCallStatus(row["status"]),
        outcome=row["outcome"],
        call_id=row["call_id"],
        call_initiated_time=row["call_initiated_time"],
        call_end_time=row["call_end_time"],
        cost=row["cost"],
        is_locked=row.get("is_locked", False),
        langfuse_scores=parse_json(row, "langfuse_scores"),
        execution_mode=ExecutionMode(row.get("execution_mode", "TELEPHONY")),
        call_direction=CallDirection(row.get("call_direction", "OUTBOUND")),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

"""
Database query functions for the application.
"""
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from app.schemas import Workflow, LeadCallStatus, RequestedBy
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
    call_initiated_time: Optional[datetime] = None,
    call_end_time: Optional[datetime] = None,
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
            "cost",
            "created_at",
            "updated_at"
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) RETURNING *;
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
        cost,
        datetime.now(),
        datetime.now()
    ]
    
    return text, values

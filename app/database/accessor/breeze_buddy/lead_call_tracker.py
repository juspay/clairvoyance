"""
Database accessor functions for the application.
"""
from typing import Any, Dict, List, Optional
from datetime import datetime
import asyncpg
from app.core.logger import logger
from app.schemas import LeadCallTracker, Workflow, RequestedBy
from app.database.queries.main import run_parameterized_query
from app.database.accessor.decoder import decode_lead_call_tracker
from app.database.queries.breeze_buddy.lead_call_tracker import (
    insert_lead_call_tracker_query,
)

def get_row_count(result: Optional[List[asyncpg.Record]]) -> int:
    """
    Get the number of rows in the result.
    """
    return len(result) if result else 0

async def create_lead_call_tracker(
    id: str,
    merchant_id: RequestedBy,
    workflow: Workflow,
    next_attempt_at: Optional[datetime],
    payload: Optional[Dict[str, Any]],
    meta_data: Optional[Dict[str, Any]] = None,
    call_initiated_time: Optional[datetime] = None,
    call_end_time: Optional[datetime] = None,
    cost: Optional[float] = None,
) -> Optional[LeadCallTracker]:
    """
    Create a new lead call tracker record.
    """
    logger.info(f"Creating lead call tracker for merchant ID: {merchant_id}")
    
    try:
        query_text, values = insert_lead_call_tracker_query(
            id=id,
            merchant_id=merchant_id,
            workflow=workflow,
            next_attempt_at=next_attempt_at,
            payload=payload,
            meta_data=meta_data,
            call_initiated_time=call_initiated_time,
            call_end_time=call_end_time,
            cost=cost,
        )
        
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result)
            logger.info(f"Lead call tracker created successfully: {decoded_result}")
            return decoded_result
        
        logger.error("Failed to create lead call tracker")
        return None
        
    except Exception as e:
        logger.error(f"Error creating lead call tracker: {e}")
        return None

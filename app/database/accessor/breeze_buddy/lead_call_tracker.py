"""
Database accessor functions for the application.
"""
from typing import Any, Dict, List, Optional
from datetime import datetime
import asyncpg
from app.core.logger import logger
from app.schemas import LeadCallTracker, LeadCallStatus, Workflow, RequestedBy
from app.database.queries import run_parameterized_query
from app.database.decoder.breeze_buddy.lead_call_tracker import decode_lead_call_tracker
from app.database.queries.breeze_buddy.lead_call_tracker import (
    insert_lead_call_tracker_query,
    get_leads_based_on_status_and_next_attempt_query,
    update_lead_call_details_query,
    get_lead_by_call_id_query,
    update_lead_call_completion_details_query,
    update_lead_call_recording_url_query,
)
from app.schemas import LeadCallOutcome

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
    attempt_count: int = 0,
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
            attempt_count=attempt_count,
            cost=cost,
        )
        
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(f"Lead call tracker created successfully: {decoded_result}")
            return decoded_result
        
        logger.error("Failed to create lead call tracker")
        return None
        
    except Exception as e:
        logger.error(f"Error creating lead call tracker: {e}")
        return None

async def get_leads_based_on_status_and_next_attempt(status: LeadCallStatus, time: datetime) -> List[LeadCallTracker]:
    """
    Get leads based on status and next attempt time.
    """
    logger.info(f"Getting leads with status {status} and next attempt at {time}")
    
    try:
        query_text, values = get_leads_based_on_status_and_next_attempt_query(status, time)
        result = await run_parameterized_query(query_text, values)
        if result:
            return [decode_lead_call_tracker(row) for row in result]
        return []
    except Exception as e:
        logger.error(f"Error getting leads: {e}")
        return []

async def update_lead_call_details(id: str, status: LeadCallStatus, call_id: str, call_initiated_time: datetime, outbound_number_id: str) -> Optional[LeadCallTracker]:
    """
    Update lead call details.
    """
    logger.info(f"Updating lead {id} with status {status} and call ID {call_id}")
    
    try:
        query_text, values = update_lead_call_details_query(id, status, call_id, call_initiated_time, outbound_number_id)
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(f"Lead updated successfully: {decoded_result}")
            return decoded_result
        
        logger.error("Failed to update lead")
        return None
        
    except Exception as e:
        logger.error(f"Error updating lead: {e}")
        return None

async def get_lead_by_call_id(call_id: str) -> Optional[LeadCallTracker]:
    """
    Get lead by call ID.
    """
    logger.info(f"Getting lead with call ID {call_id}")
    
    try:
        query_text, values = get_lead_by_call_id_query(call_id)
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(f"Lead found: {decoded_result}")
            return decoded_result
        
        logger.error("Lead not found")
        return None
        
    except Exception as e:
        logger.error(f"Error getting lead: {e}")
        return None


async def update_lead_call_recording_url(call_id: str, recording_url: str) -> Optional[LeadCallTracker]:
    """
    Update lead call recording url.
    """
    logger.info(f"Updating lead with call ID {call_id} with recording url")
    
    try:
        query_text, values = update_lead_call_recording_url_query(call_id, recording_url)
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(f"Lead updated successfully: {decoded_result}")
            return decoded_result
        
        logger.error("Failed to update lead")
        return None
        
    except Exception as e:
        logger.error(f"Error updating lead: {e}")
        return None


async def update_lead_call_completion_details(
    id: str,
    status: LeadCallStatus,
    outcome: LeadCallOutcome,
    meta_data: Dict[str, Any],
    call_end_time: datetime,
) -> Optional[LeadCallTracker]:
    """
    Update lead call completion details.
    """
    logger.info(f"Updating lead call completion details for ID: {id}")
    
    try:
        query_text, values = update_lead_call_completion_details_query(
            id, status, outcome, meta_data, call_end_time
        )
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(f"Lead call completion details updated successfully: {decoded_result}")
            return decoded_result
        
        logger.error("Failed to update lead call completion details")
        return None
        
    except Exception as e:
        logger.error(f"Error updating lead call completion details: {e}")
        return None

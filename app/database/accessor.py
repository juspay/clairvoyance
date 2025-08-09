"""
Database accessor functions for the application.
"""
from typing import Any, Dict, List, Optional
import asyncpg
import json
from datetime import datetime
from app.core.logger import logger
from app.database import get_db_connection
from app.schemas import CallDataResponse, CallOutcome, CallStatus, RequestedBy
from app.database.queries import (
    insert_call_data_query,
    get_call_data_by_id_query,
    get_call_data_by_call_id_query,
    update_call_data_status_query,
    update_call_data_outcome_query,
    get_call_data_by_status_query,
    get_call_data_by_provider_query,
    get_call_data_by_requested_by_query,
    delete_call_data_query,
    get_all_call_data_query,
    update_call_data_call_id_query,
    complete_call_data_update_query,
)

# Helper function to execute parameterized queries
async def run_parameterized_query(query_text: str, values: List[Any]) -> Optional[List[asyncpg.Record]]:
    """
    Execute a parameterized query and return the results.
    """
    try:
        async for conn in get_db_connection():
            if query_text.strip().upper().startswith('SELECT'):
                result = await conn.fetch(query_text, *values)
                return result
            else:
                result = await conn.fetchrow(query_text, *values)
                return [result] if result else None
    except Exception as e:
        logger.error(f"Database query error: {e}")
        return None

def get_row_count(result: Optional[List[asyncpg.Record]]) -> int:
    """
    Get the number of rows in the result.
    """
    return len(result) if result else 0

def decode_call_data(result: List[asyncpg.Record]) -> Optional[CallDataResponse]:
    """
    Decode call data from database result using Pydantic model.
    """
    if not result or len(result) == 0:
        return None
    
    row = result[0]
    return CallDataResponse(
        id=row["id"],
        outcome=row["outcome"],
        transcription=row["transcription"] if isinstance(row["transcription"], dict) else json.loads(row["transcription"]) if row["transcription"] else None,
        call_start_time=row["call_start_time"].isoformat() if row["call_start_time"] else "",
        call_end_time=row["call_end_time"].isoformat() if row["call_end_time"] else None,
        call_id=row["call_id"],
        provider=row["provider"],
        status=row["status"],
        requested_by=row["requested_by"],
        call_payload=row["call_payload"] if isinstance(row["call_payload"], dict) else json.loads(row["call_payload"]) if row["call_payload"] else None,
        assigned_number=row["assigned_number"],
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
    )

def decode_call_data_list(result: List[asyncpg.Record]) -> List[CallDataResponse]:
    """
    Decode multiple call data records from database result using Pydantic models.
    """
    if not result:
        return []
    
    return [
        CallDataResponse(
            id=row["id"],
            outcome=row["outcome"],
            transcription=row["transcription"] if isinstance(row["transcription"], dict) else json.loads(row["transcription"]) if row["transcription"] else None,
            call_start_time=row["call_start_time"].isoformat() if row["call_start_time"] else "",
            call_end_time=row["call_end_time"].isoformat() if row["call_end_time"] else None,
            call_id=row["call_id"],
            provider=row["provider"],
            status=row["status"],
            requested_by=row["requested_by"],
            call_payload=row["call_payload"] if isinstance(row["call_payload"], dict) else json.loads(row["call_payload"]) if row["call_payload"] else None,
            assigned_number=row["assigned_number"],
            created_at=row["created_at"].isoformat() if row["created_at"] else "",
            updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
        )
        for row in result
    ]

# Call Data accessor functions
async def create_call_data(
    id: str,
    outcome: Optional[CallOutcome],
    transcription: Optional[Dict[str, Any]],
    call_start_time: str,
    call_end_time: Optional[str],
    call_id: str,
    provider: str,
    status: CallStatus,
    requested_by: RequestedBy,
    call_payload: Optional[Dict[str, Any]],
    assigned_number: Optional[str] = None
) -> Optional[CallDataResponse]:
    """
    Create a new call data record.
    """
    logger.info(f"Creating call data with ID: {id}, call_id: {call_id}")
    
    try:
        query_text, values = insert_call_data_query(
            id=id,
            outcome=outcome,
            transcription=transcription,
            call_start_time=call_start_time,
            call_end_time=call_end_time,
            call_id=call_id,
            provider=provider,
            status=status,
            requested_by=requested_by,
            call_payload=call_payload,
            assigned_number=assigned_number
        )
        
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_call_data(result)
            logger.info(f"Call data created successfully: {decoded_result}")
            return decoded_result
        
        logger.error("Failed to create call data")
        return None
        
    except Exception as e:
        logger.error(f"Error creating call data: {e}")
        return None

async def get_call_data_by_id(call_data_id: str) -> Optional[CallDataResponse]:
    """
    Get call data by ID.
    """
    logger.info(f"Getting call data by ID: {call_data_id}")
    
    try:
        query_text, values = get_call_data_by_id_query(call_data_id)
        result = await run_parameterized_query(query_text, values)
        
        if result and get_row_count(result) > 0:
            decoded_result = decode_call_data(result)
            logger.info(f"Call data found: {decoded_result}")
            return decoded_result
        
        logger.info(f"No call data found with ID: {call_data_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error getting call data by ID: {e}")
        return None

async def get_call_data_by_call_id(call_id: str) -> Optional[CallDataResponse]:
    """
    Get call data by call ID.
    """
    logger.info(f"Getting call data by call ID: {call_id}")
    
    try:
        query_text, values = get_call_data_by_call_id_query(call_id)
        result = await run_parameterized_query(query_text, values)
        
        if result and get_row_count(result) > 0:
            decoded_result = decode_call_data(result)
            logger.info(f"Call data found: {decoded_result}")
            return decoded_result
        
        logger.info(f"No call data found with call ID: {call_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error getting call data by call ID: {e}")
        return None

async def update_call_data_status(call_data_id: str, status: CallStatus) -> Optional[CallDataResponse]:
    """
    Update call data status.
    """
    logger.info(f"Updating call data status for ID: {call_data_id}, new status: {status}")
    
    try:
        query_text, values = update_call_data_status_query(call_data_id, status)
        result = await run_parameterized_query(query_text, values)
        
        if result and get_row_count(result) > 0:
            decoded_result = decode_call_data(result)
            logger.info(f"Call data status updated: {decoded_result}")
            return decoded_result
        
        logger.error(f"Failed to update call data status for ID: {call_data_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error updating call data status: {e}")
        return None

async def update_call_data_outcome(call_data_id: str, outcome: CallOutcome) -> Optional[CallDataResponse]:
    """
    Update call data outcome.
    """
    logger.info(f"Updating call data outcome for ID: {call_data_id}, new outcome: {outcome}")
    
    try:
        query_text, values = update_call_data_outcome_query(call_data_id, outcome)
        result = await run_parameterized_query(query_text, values)
        
        if result and get_row_count(result) > 0:
            decoded_result = decode_call_data(result)
            logger.info(f"Call data outcome updated: {decoded_result}")
            return decoded_result
        
        logger.error(f"Failed to update call data outcome for ID: {call_data_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error updating call data outcome: {e}")
        return None

async def update_call_data_call_id(call_data_id: str, call_id: str) -> Optional[CallDataResponse]:
    """
    Update call data call_id.
    """
    logger.info(f"Updating call data call_id for ID: {call_data_id}, new call_id: {call_id}")
    
    try:
        query_text, values = update_call_data_call_id_query(call_data_id, call_id)
        result = await run_parameterized_query(query_text, values)
        
        if result and get_row_count(result) > 0:
            decoded_result = decode_call_data(result)
            logger.info(f"Call data call_id updated: {decoded_result}")
            return decoded_result
        
        logger.error(f"Failed to update call data call_id for ID: {call_data_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error updating call data call_id: {e}")
        return None

async def complete_call_data_update(
    call_data_id: str,
    outcome: Optional[CallOutcome] = None,
    status: Optional[CallStatus] = None,
    transcription: Optional[Dict[str, Any]] = None,
    call_end_time: Optional[str] = None
) -> Optional[CallDataResponse]:
    """
    Complete call data update with outcome, status, transcription, and call_end_time.
    """
    logger.info(f"Completing call data update for ID: {call_data_id}")
    
    try:
        query_text, values = complete_call_data_update_query(
            call_data_id=call_data_id,
            outcome=outcome,
            status=status,
            transcription=transcription,
            call_end_time=call_end_time
        )
        
        result = await run_parameterized_query(query_text, values)
        
        if result and get_row_count(result) > 0:
            decoded_result = decode_call_data(result)
            logger.info(f"Call data completion update successful: {decoded_result}")
            return decoded_result
        
        logger.error(f"Failed to complete call data update for ID: {call_data_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error completing call data update: {e}")
        return None

async def get_call_data_by_status(status: CallStatus) -> List[CallDataResponse]:
    """
    Get call data by status.
    """
    logger.info(f"Getting call data by status: {status}")
    
    try:
        query_text, values = get_call_data_by_status_query(status)
        result = await run_parameterized_query(query_text, values)
        
        if result:
            decoded_result = decode_call_data_list(result)
            logger.info(f"Found {len(decoded_result)} call data records with status: {status}")
            return decoded_result
        
        logger.info(f"No call data found with status: {status}")
        return []
        
    except Exception as e:
        logger.error(f"Error getting call data by status: {e}")
        return []

async def get_call_data_by_provider(provider: str) -> List[CallDataResponse]:
    """
    Get call data by provider.
    """
    logger.info(f"Getting call data by provider: {provider}")
    
    try:
        query_text, values = get_call_data_by_provider_query(provider)
        result = await run_parameterized_query(query_text, values)
        
        if result:
            decoded_result = decode_call_data_list(result)
            logger.info(f"Found {len(decoded_result)} call data records with provider: {provider}")
            return decoded_result
        
        logger.info(f"No call data found with provider: {provider}")
        return []
        
    except Exception as e:
        logger.error(f"Error getting call data by provider: {e}")
        return []

async def get_call_data_by_requested_by(requested_by: RequestedBy) -> List[CallDataResponse]:
    """
    Get call data by requested_by.
    """
    logger.info(f"Getting call data by requested_by: {requested_by}")
    
    try:
        query_text, values = get_call_data_by_requested_by_query(requested_by)
        result = await run_parameterized_query(query_text, values)
        
        if result:
            decoded_result = decode_call_data_list(result)
            logger.info(f"Found {len(decoded_result)} call data records with requested_by: {requested_by}")
            return decoded_result
        
        logger.info(f"No call data found with requested_by: {requested_by}")
        return []
        
    except Exception as e:
        logger.error(f"Error getting call data by requested_by: {e}")
        return []

async def delete_call_data(call_data_id: str) -> bool:
    """
    Delete call data by ID.
    """
    logger.info(f"Deleting call data with ID: {call_data_id}")
    
    try:
        query_text, values = delete_call_data_query(call_data_id)
        result = await run_parameterized_query(query_text, values)
        
        if result is not None:
            logger.info(f"Call data deleted successfully: {call_data_id}")
            return True
        
        logger.error(f"Failed to delete call data with ID: {call_data_id}")
        return False
        
    except Exception as e:
        logger.error(f"Error deleting call data: {e}")
        return False

async def get_all_call_data() -> List[CallDataResponse]:
    """
    Get all call data.
    """
    logger.info("Getting all call data")
    
    try:
        query_text, values = get_all_call_data_query()
        result = await run_parameterized_query(query_text, values)
        
        if result:
            decoded_result = decode_call_data_list(result)
            logger.info(f"Found {len(decoded_result)} call data records")
            return decoded_result
        
        logger.info("No call data found")
        return []
        
    except Exception as e:
        logger.error(f"Error getting all call data: {e}")
        return []

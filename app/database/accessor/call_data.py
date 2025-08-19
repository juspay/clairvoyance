"""
Database accessor functions for the application.
"""
from typing import Any, Dict, List, Optional
import asyncpg
from app.core.logger import logger
from app.schemas import CallDataResponse, CallOutcome, CallStatus, RequestedBy
from app.database.queries.main import run_parameterized_query
from app.database.accessor.decoder import decode_call_data, decode_call_data_list
from app.database.queries.call_data import (
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

def get_row_count(result: Optional[List[asyncpg.Record]]) -> int:
    """
    Get the number of rows in the result.
    """
    return len(result) if result else 0

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

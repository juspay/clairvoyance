"""
Database accessor functions for the application.
"""
from typing import Any, Dict, List, Optional
import asyncpg
from app.core.logger import logger
from app.schemas import OutboundNumber, OutboundNumberStatus, CallProvider
from app.database.queries.main import run_parameterized_query
from app.database.accessor.decoder import decode_outbound_number, decode_outbound_number_list
from app.database.queries.outbound_number import (
    insert_outbound_number_query,
    get_outbound_number_by_id_query,
    update_outbound_number_status_query,
    disable_outbound_number_query,
    get_all_outbound_numbers_query,
    get_outbound_number_based_on_status_and_provider_query,
)

def get_row_count(result: Optional[List[asyncpg.Record]]) -> int:
    """
    Get the number of rows in the result.
    """
    return len(result) if result else 0

async def create_outbound_number(
    id: str,
    number: str,
    provider: CallProvider,
    status: OutboundNumberStatus,
    channels: Optional[int] = None,
    maximum_channels: Optional[int] = None,
) -> Optional[OutboundNumber]:
    """
    Create a new outbound number record.
    """
    logger.info(f"Creating outbound number with ID: {id}")
    
    try:
        query_text, values = insert_outbound_number_query(
            id=id,
            number=number,
            provider=provider,
            status=status,
            channels=channels,
            maximum_channels=maximum_channels,
        )
        
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_outbound_number(result)
            logger.info(f"Outbound number created successfully: {decoded_result}")
            return decoded_result
        
        logger.error("Failed to create outbound number")
        return None
        
    except Exception as e:
        logger.error(f"Error creating outbound number: {e}")
        return None

async def get_outbound_number_by_id(outbound_number_id: str) -> Optional[OutboundNumber]:
    """
    Get outbound number by ID.
    """
    logger.info(f"Getting outbound number by ID: {outbound_number_id}")
    
    try:
        query_text, values = get_outbound_number_by_id_query(outbound_number_id)
        result = await run_parameterized_query(query_text, values)
        
        if result and get_row_count(result) > 0:
            decoded_result = decode_outbound_number(result)
            logger.info(f"Outbound number found: {decoded_result}")
            return decoded_result
        
        logger.info(f"No outbound number found with ID: {outbound_number_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error getting outbound number by ID: {e}")
        return None

async def update_outbound_number_status(outbound_number_id: str, status: OutboundNumberStatus) -> Optional[OutboundNumber]:
    """
    Update outbound number status.
    """
    logger.info(f"Updating outbound number status for ID: {outbound_number_id}, new status: {status}")
    
    try:
        query_text, values = update_outbound_number_status_query(outbound_number_id, status)
        result = await run_parameterized_query(query_text, values)
        
        if result and get_row_count(result) > 0:
            decoded_result = decode_outbound_number(result)
            logger.info(f"Outbound number status updated: {decoded_result}")
            return decoded_result
        
        logger.error(f"Failed to update outbound number status for ID: {outbound_number_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error updating outbound number status: {e}")
        return None

async def disable_outbound_number(outbound_number_id: str) -> Optional[OutboundNumber]:
    """
    Disable outbound number by ID.
    """
    logger.info(f"Disabling outbound number with ID: {outbound_number_id}")
    
    try:
        query_text, values = disable_outbound_number_query(outbound_number_id)
        result = await run_parameterized_query(query_text, values)
        
        if result and get_row_count(result) > 0:
            decoded_result = decode_outbound_number(result)
            logger.info(f"Outbound number disabled successfully: {decoded_result}")
            return decoded_result
        
        logger.error(f"Failed to disable outbound number with ID: {outbound_number_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error disabling outbound number: {e}")
        return None

async def get_all_outbound_numbers() -> List[OutboundNumber]:
    """
    Get all outbound numbers.
    """
    logger.info("Getting all outbound numbers")
    
    try:
        query_text, values = get_all_outbound_numbers_query()
        result = await run_parameterized_query(query_text, values)
        
        if result:
            decoded_result = decode_outbound_number_list(result)
            logger.info(f"Found {len(decoded_result)} outbound number records")
            return decoded_result
        
        logger.info("No outbound numbers found")
        return []
        
    except Exception as e:
        logger.error(f"Error getting all outbound numbers: {e}")
        return []

async def get_outbound_number_based_on_status_and_provider(status: OutboundNumberStatus, provider: CallProvider) -> Optional[OutboundNumber]:
    """
    Get outbound number by status and provider.
    """
    logger.info(f"Getting outbound number with status: {status} and provider: {provider}")
    
    try:
        query_text, values = get_outbound_number_based_on_status_and_provider_query(status, provider)
        result = await run_parameterized_query(query_text, values)
        
        if result and get_row_count(result) > 0:
            decoded_result = decode_outbound_number(result)
            logger.info(f"Outbound number found: {decoded_result}")
            return decoded_result
        
        logger.info(f"No outbound number found with status: {status} and provider: {provider}")
        return None
        
    except Exception as e:
        logger.error(f"Error getting outbound number: {e}")
        return None

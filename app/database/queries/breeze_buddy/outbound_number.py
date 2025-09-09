"""
Database query functions for the application.
"""
from typing import Any, List, Optional, Tuple
from datetime import datetime
from app.schemas import OutboundNumberStatus, CallProvider

# Table names
OUTBOUND_NUMBER_TABLE = "outbound_number"

# Outbound number queries
def insert_outbound_number_query(
    id: str,
    number: str,
    provider: CallProvider,
    status: OutboundNumberStatus,
    channels: Optional[int] = None,
    maximum_channels: Optional[int] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to insert outbound number record.
    """
    text = f"""
        INSERT INTO "{OUTBOUND_NUMBER_TABLE}"
        (
            "id",
            "number",
            "provider",
            "status",
            "channels",
            "maximum_channels",
            "created_at",
            "updated_at"
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *;
    """
    
    values = [
        id,
        number,
        provider.value,
        status.value,
        channels,
        maximum_channels,
        datetime.now(),
        datetime.now()
    ]
    
    return text, values

def get_outbound_number_by_id_query(outbound_number_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get outbound number by ID.
    """
    text = f'SELECT * FROM "{OUTBOUND_NUMBER_TABLE}" WHERE "id" = $1;'
    values = [outbound_number_id]
    return text, values

def update_outbound_number_status_query(outbound_number_id: str, status: OutboundNumberStatus) -> Tuple[str, List[Any]]:
    """
    Generate query to update outbound number status.
    """
    text = f"""
        UPDATE "{OUTBOUND_NUMBER_TABLE}" 
        SET "status" = $2, "updated_at" = NOW() 
        WHERE "id" = $1 
        RETURNING *;
    """
    values = [outbound_number_id, status.value]
    return text, values

def disable_outbound_number_query(outbound_number_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to disable outbound number by ID.
    """
    text = f"""
        UPDATE "{OUTBOUND_NUMBER_TABLE}" 
        SET "status" = $2, "updated_at" = NOW() 
        WHERE "id" = $1 
        RETURNING *;
    """
    values = [outbound_number_id, OutboundNumberStatus.DISABLED.value]
    return text, values

def get_all_outbound_numbers_query() -> Tuple[str, List[Any]]:
    """
    Generate query to get all outbound numbers.
    """
    text = f"""
        SELECT * FROM "{OUTBOUND_NUMBER_TABLE}" 
        ORDER BY "created_at" DESC;
    """
    values = []
    return text, values

def get_outbound_number_based_on_status_and_provider_query(status: OutboundNumberStatus, provider: CallProvider) -> Tuple[str, List[Any]]:
    """
    Generate query to get outbound number by status and provider.
    """
    text = f"""
        SELECT * FROM "{OUTBOUND_NUMBER_TABLE}" 
        WHERE "status" = $1 AND "provider" = $2
        ORDER BY "created_at" DESC;
        LIMIT 1;
    """
    values = [status.value, provider.value]
    return text, values

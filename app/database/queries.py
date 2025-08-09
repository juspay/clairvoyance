"""
Database query functions for the application.
"""
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID
import asyncpg
from datetime import datetime
import json
from app.core.logger import logger
from app.schemas import CallOutcome, CallStatus, RequestedBy

# Table names
CALL_DATA_TABLE = "call_data"

# Call data queries
def insert_call_data_query(
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
) -> Tuple[str, List[Any]]:
    """
    Generate query to insert call data record.
    """
    text = f"""
        INSERT INTO "{CALL_DATA_TABLE}"
        (
            "id",
            "outcome",
            "transcription",
            "call_start_time",
            "call_end_time",
            "call_id",
            "provider",
            "status",
            "requested_by",
            "call_payload",
            "assigned_number",
            "created_at",
            "updated_at"
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING *;
    """
    
    call_start_dt = datetime.fromisoformat(call_start_time.replace('Z', '+00:00')) if call_start_time else None
    call_end_dt = datetime.fromisoformat(call_end_time.replace('Z', '+00:00')) if call_end_time else None
    
    values = [
        id,
        outcome.value if outcome else None,
        json.dumps(transcription) if transcription else None,
        call_start_dt,
        call_end_dt,
        call_id,
        provider,
        status.value,
        requested_by.value,
        json.dumps(call_payload) if call_payload else None,
        assigned_number,
        datetime.now(),
        datetime.now()
    ]
    
    return text, values

def get_call_data_by_id_query(call_data_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get call data by ID.
    """
    text = f'SELECT * FROM "{CALL_DATA_TABLE}" WHERE "id" = $1;'
    values = [call_data_id]
    return text, values

def get_call_data_by_call_id_query(call_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get call data by call ID.
    """
    text = f'SELECT * FROM "{CALL_DATA_TABLE}" WHERE "call_id" = $1;'
    values = [call_id]
    return text, values

def update_call_data_status_query(call_data_id: str, status: CallStatus) -> Tuple[str, List[Any]]:
    """
    Generate query to update call data status.
    """
    text = f"""
        UPDATE "{CALL_DATA_TABLE}" 
        SET "status" = $2, "updated_at" = NOW() 
        WHERE "id" = $1 
        RETURNING *;
    """
    values = [call_data_id, status.value]
    return text, values

def update_call_data_outcome_query(call_data_id: str, outcome: CallOutcome) -> Tuple[str, List[Any]]:
    """
    Generate query to update call data outcome.
    """
    text = f"""
        UPDATE "{CALL_DATA_TABLE}" 
        SET "outcome" = $2, "updated_at" = NOW() 
        WHERE "id" = $1 
        RETURNING *;
    """
    values = [call_data_id, outcome.value]
    return text, values

def get_call_data_by_status_query(status: CallStatus) -> Tuple[str, List[Any]]:
    """
    Generate query to get call data by status.
    """
    text = f"""
        SELECT * FROM "{CALL_DATA_TABLE}" 
        WHERE "status" = $1 
        ORDER BY "created_at" DESC;
    """
    values = [status.value]
    return text, values

def get_call_data_by_provider_query(provider: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get call data by provider.
    """
    text = f"""
        SELECT * FROM "{CALL_DATA_TABLE}" 
        WHERE "provider" = $1 
        ORDER BY "created_at" DESC;
    """
    values = [provider]
    return text, values

def get_call_data_by_requested_by_query(requested_by: RequestedBy) -> Tuple[str, List[Any]]:
    """
    Generate query to get call data by requested_by.
    """
    text = f"""
        SELECT * FROM "{CALL_DATA_TABLE}" 
        WHERE "requested_by" = $1 
        ORDER BY "created_at" DESC;
    """
    values = [requested_by.value]
    return text, values

def delete_call_data_query(call_data_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to delete call data by ID.
    """
    text = f'DELETE FROM "{CALL_DATA_TABLE}" WHERE "id" = $1;'
    values = [call_data_id]
    return text, values

def update_call_data_call_id_query(call_data_id: str, call_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to update call data call_id.
    """
    text = f"""
        UPDATE "{CALL_DATA_TABLE}" 
        SET "call_id" = $2, "updated_at" = NOW() 
        WHERE "id" = $1 
        RETURNING *;
    """
    values = [call_data_id, call_id]
    return text, values

def complete_call_data_update_query(
    call_data_id: str,
    outcome: Optional[CallOutcome] = None,
    status: Optional[CallStatus] = None,
    transcription: Optional[Dict[str, Any]] = None,
    call_end_time: Optional[str] = None
) -> Tuple[str, List[Any]]:
    """
    Generate query to complete call data update with outcome, status, transcription, and call_end_time.
    """
    # Build dynamic query based on what needs to be updated
    set_clauses = []
    values = [call_data_id]
    param_count = 2
    
    if outcome is not None:
        set_clauses.append(f'"outcome" = ${param_count}')
        values.append(outcome.value)
        param_count += 1
        
    if status is not None:
        set_clauses.append(f'"status" = ${param_count}')
        values.append(status.value)
        param_count += 1
        
    if transcription is not None:
        set_clauses.append(f'"transcription" = ${param_count}')
        values.append(json.dumps(transcription))
        param_count += 1
        
    if call_end_time is not None:
        set_clauses.append(f'"call_end_time" = ${param_count}')
        call_end_dt = datetime.fromisoformat(call_end_time.replace('Z', '+00:00')) if call_end_time else None
        values.append(call_end_dt)
        param_count += 1
    
    # Always update the updated_at timestamp
    set_clauses.append('"updated_at" = NOW()')
    
    text = f"""
        UPDATE "{CALL_DATA_TABLE}" 
        SET {', '.join(set_clauses)}
        WHERE "id" = $1 
        RETURNING *;
    """
    
    return text, values

def get_all_call_data_query() -> Tuple[str, List[Any]]:
    """
    Generate query to get all call data.
    """
    text = f"""
        SELECT * FROM "{CALL_DATA_TABLE}" 
        ORDER BY "created_at" DESC;
    """
    values = []
    return text, values

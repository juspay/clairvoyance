"""
Decoder functions for the application.
"""
from typing import List, Optional
import asyncpg
from app.schemas import CallDataResponse, OutboundNumber, OutboundNumber, OutboundNumberStatus, CallProvider
from app.utils.common import parse_json

def decode_outbound_number(result: List[asyncpg.Record]) -> Optional[OutboundNumber]:
    """
    Decode outbound number from database result using Pydantic model.
    """
    if not result or len(result) == 0:
        return None
    
    row = result[0]
    return OutboundNumber(
        id=row["id"],
        number=row["number"],
        provider=CallProvider(row["provider"]),
        status=OutboundNumberStatus(row["status"]),
        channels=row["channels"],
        maximum_channels=row["maximum_channels"],
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
    )

def decode_outbound_number_list(result: List[asyncpg.Record]) -> List[OutboundNumber]:
    """
    Decode multiple outbound number records from database result using Pydantic models.
    """
    if not result:
        return []
    
    return [
        OutboundNumber(
            id=row["id"],
            number=row["number"],
            provider=CallProvider(row["provider"]),
            status=OutboundNumberStatus(row["status"]),
            channels=row["channels"],
            maximum_channels=row["maximum_channels"],
            created_at=row["created_at"].isoformat() if row["created_at"] else "",
            updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
        )
        for row in result
    ]

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
        transcription=parse_json(row, "transcription"),
        call_start_time=row["call_start_time"].isoformat() if row["call_start_time"] else "",
        call_end_time=row["call_end_time"].isoformat() if row["call_end_time"] else None,
        call_id=row["call_id"],
        provider=row["provider"],
        status=row["status"],
        requested_by=row["requested_by"],
        workflow=row["workflow"],
        call_payload=parse_json(row, "call_payload"),
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
            transcription=parse_json(row, "transcription"),
            call_start_time=row["call_start_time"].isoformat() if row["call_start_time"] else "",
            call_end_time=row["call_end_time"].isoformat() if row["call_end_time"] else None,
            call_id=row["call_id"],
            provider=row["provider"],
            status=row["status"],
            requested_by=row["requested_by"],
            workflow=row["workflow"],
            call_payload=parse_json(row, "call_payload"),
            assigned_number=row["assigned_number"],
            created_at=row["created_at"].isoformat() if row["created_at"] else "",
            updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
        )
        for row in result
    ]

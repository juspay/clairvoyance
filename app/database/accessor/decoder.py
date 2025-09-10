"""
Decoder functions for the application.
"""
from typing import List, Optional
import asyncpg
from app.schemas import Workflow, CallDataResponse, OutboundNumber, OutboundNumber, OutboundNumberStatus, CallProvider, CallExecutionConfig, LeadCallTracker
from app.utils.common import parse_json

def decode_lead_call_tracker(result: List[asyncpg.Record]) -> Optional[LeadCallTracker]:
    """
    Decode lead call tracker from database result using Pydantic model.
    """
    if not result or len(result) == 0:
        return None
    
    row = result[0]
    return LeadCallTracker(
        id=row["id"],
        outbound_number_id=row["outbound_number_id"],
        merchant_id=row["merchant_id"],
        workflow=row["workflow"],
        attempt_count=row["attempt_count"],
        next_attempt_at=row["next_attempt_at"],
        payload=parse_json(row, "payload"),
        meta_data=parse_json(row, "meta_data"),
        recording_url=row["recording_url"],
        status=row["status"],
        outcome=row["outcome"],
        call_id=row["call_id"],
        call_initiated_time=row["call_initiated_time"],
        call_end_time=row["call_end_time"],
        cost=row["cost"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

def decode_call_execution_config_list(result: List[asyncpg.Record]) -> List[CallExecutionConfig]:
    """
    Decode multiple call execution config records from database result using Pydantic models.
    """
    if not result:
        return []
    
    return [
        CallExecutionConfig(
            id=row["id"],
            initial_offset=row["initial_offset"],
            retry_offset=row["retry_offset"],
            call_start_time=row["call_start_time"],
            call_end_time=row["call_end_time"],
            max_retry=row["max_retry"],
            calling_provider=CallProvider(row["calling_provider"]),
            merchant_id=row["merchant_id"],
            workflow=Workflow(row["workflow"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in result
    ]

def decode_call_execution_config(result: List[asyncpg.Record]) -> Optional[CallExecutionConfig]:
    """
    Decode call execution config from database result using Pydantic model.
    """
    if not result or len(result) == 0:
        return None
    
    row = result[0]
    return CallExecutionConfig(
        id=row["id"],
        initial_offset=row["initial_offset"],
        retry_offset=row["retry_offset"],
        call_start_time=row["call_start_time"],
        call_end_time=row["call_end_time"],
        max_retry=row["max_retry"],
        calling_provider=CallProvider(row["calling_provider"]),
        merchant_id=row["merchant_id"],
        workflow=Workflow(row["workflow"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

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
        created_at=row["created_at"],
        updated_at=row["updated_at"],
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
            created_at=row["created_at"],
            updated_at=row["updated_at"],
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
        call_start_time=row["call_start_time"],
        call_end_time=row["call_end_time"],
        call_id=row["call_id"],
        provider=row["provider"],
        status=row["status"],
        requested_by=row["requested_by"],
        workflow=row["workflow"],
        call_payload=parse_json(row, "call_payload"),
        assigned_number=row["assigned_number"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
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
            call_start_time=row["call_start_time"],
            call_end_time=row["call_end_time"],
            call_id=row["call_id"],
            provider=row["provider"],
            status=row["status"],
            requested_by=row["requested_by"],
            workflow=row["workflow"],
            call_payload=parse_json(row, "call_payload"),
            assigned_number=row["assigned_number"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in result
    ]

"""
Decoder functions for call execution config.
"""

from typing import List, Optional
import asyncpg
from app.schemas import Workflow, CallExecutionConfig, CallProvider


def decode_call_execution_config_list(
    result: List[asyncpg.Record],
) -> List[CallExecutionConfig]:
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


def decode_call_execution_config(
    result: List[asyncpg.Record],
) -> Optional[CallExecutionConfig]:
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

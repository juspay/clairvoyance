"""
Database query functions for the application.
"""

from datetime import datetime, time
from typing import Any, List, Tuple

from app.schemas import CallProvider, Workflow

# Table names
CALL_EXECUTION_CONFIG_TABLE = "call_execution_config"


# Call execution config queries
def insert_call_execution_config_query(
    id: str,
    initial_offset: int,
    retry_offset: int,
    call_start_time: time,
    call_end_time: time,
    max_retry: int,
    calling_provider: CallProvider,
    merchant_id: str,
    workflow: Workflow,
) -> Tuple[str, List[Any]]:
    """
    Generate query to insert call execution config record.
    """
    text = f"""
        INSERT INTO "{CALL_EXECUTION_CONFIG_TABLE}"
        (
            "id",
            "initial_offset",
            "retry_offset",
            "call_start_time",
            "call_end_time",
            "max_retry",
            "calling_provider",
            "merchant_id",
            "workflow",
            "created_at",
            "updated_at"
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING *;
    """

    values = [
        id,
        initial_offset,
        retry_offset,
        call_start_time,
        call_end_time,
        max_retry,
        calling_provider.value,
        merchant_id,
        workflow.value,
        datetime.now(),
        datetime.now(),
    ]

    return text, values


def get_call_execution_config_by_merchant_id_query(
    merchant_id: str,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get call execution config by merchant ID.
    """
    text = f'SELECT * FROM "{CALL_EXECUTION_CONFIG_TABLE}" WHERE "merchant_id" = $1;'
    values = [merchant_id]
    return text, values

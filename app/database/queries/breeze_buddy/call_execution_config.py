"""
Database query functions for the application.
"""

from datetime import datetime, time
from typing import Any, List, Optional, Tuple

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
    shop_identifier: Optional[str],
    enable_international_call: bool,
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
            "shop_identifier",
            "enable_international_call",
            "created_at",
            "updated_at"
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING *;
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
        shop_identifier,
        enable_international_call,
        datetime.now(),
        datetime.now(),
    ]

    return text, values


def get_call_execution_config_by_merchant_id_query(
    merchant_id: str,
    shop_identifier: Optional[str],
) -> Tuple[str, List[Any]]:
    """
    Generate query to get call execution config by merchant ID and shop identifier.
    """
    if shop_identifier:
        text = f'SELECT * FROM "{CALL_EXECUTION_CONFIG_TABLE}" WHERE "merchant_id" = $1 AND "shop_identifier" = $2;'
        values: List[Any] = [merchant_id, shop_identifier]
    else:
        text = f'SELECT * FROM "{CALL_EXECUTION_CONFIG_TABLE}" WHERE "merchant_id" = $1'
        values = [merchant_id]
    return text, values


def get_all_call_execution_configs_query() -> Tuple[str, List[Any]]:
    """
    Generate query to get all call execution configs.
    """
    text = f'SELECT * FROM "{CALL_EXECUTION_CONFIG_TABLE}";'
    values: List[Any] = []
    return text, values


def update_call_execution_config_query(
    merchant_id: str,
    workflow: Workflow,
    shop_identifier: Optional[str] = None,
    initial_offset: Optional[int] = None,
    retry_offset: Optional[int] = None,
    call_start_time: Optional[time] = None,
    call_end_time: Optional[time] = None,
    max_retry: Optional[int] = None,
    calling_provider: Optional[CallProvider] = None,
    enable_international_call: Optional[bool] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to update call execution config record based on merchant_id, workflow, and shop_identifier.
    Only updates fields that are provided (not None).
    """
    updates = []
    values: List[Any] = []
    param_count = 1

    if initial_offset is not None:
        updates.append(f'"initial_offset" = ${param_count}')
        values.append(initial_offset)
        param_count += 1

    if retry_offset is not None:
        updates.append(f'"retry_offset" = ${param_count}')
        values.append(retry_offset)
        param_count += 1

    if call_start_time is not None:
        updates.append(f'"call_start_time" = ${param_count}')
        values.append(call_start_time)
        param_count += 1

    if call_end_time is not None:
        updates.append(f'"call_end_time" = ${param_count}')
        values.append(call_end_time)
        param_count += 1

    if max_retry is not None:
        updates.append(f'"max_retry" = ${param_count}')
        values.append(max_retry)
        param_count += 1

    if calling_provider is not None:
        updates.append(f'"calling_provider" = ${param_count}')
        values.append(calling_provider.value)
        param_count += 1

    if enable_international_call is not None:
        updates.append(f'"enable_international_call" = ${param_count}')
        values.append(enable_international_call)
        param_count += 1

    # Always update the updated_at timestamp
    updates.append(f'"updated_at" = ${param_count}')
    values.append(datetime.now())
    param_count += 1

    # Build WHERE clause based on merchant_id, workflow, and shop_identifier
    values.append(merchant_id)
    merchant_id_param = param_count
    param_count += 1

    values.append(workflow.value)
    workflow_param = param_count
    param_count += 1

    if shop_identifier:
        values.append(shop_identifier)
        shop_identifier_param = param_count
        where_clause = f'"merchant_id" = ${merchant_id_param} AND "workflow" = ${workflow_param} AND "shop_identifier" = ${shop_identifier_param}'
    else:
        where_clause = f'"merchant_id" = ${merchant_id_param} AND "workflow" = ${workflow_param} AND "shop_identifier" IS NULL'

    text = f"""
        UPDATE "{CALL_EXECUTION_CONFIG_TABLE}"
        SET {', '.join(updates)}
        WHERE {where_clause}
        RETURNING *;
    """

    return text, values

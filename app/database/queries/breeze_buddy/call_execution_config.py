"""
Database query functions for the application.
"""

from datetime import datetime, time
from typing import Any, List, Optional, Tuple

from app.schemas import CallProvider

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
    template: str,
    shop_identifier: Optional[str],
    enable_international_call: bool,
    enable_calling: bool = True,
    template_id: Optional[str] = None,
    pre_checks: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to insert call execution config record.

    Args:
        template_id: UUID of the template (preferred, for referential integrity)
        template: Name of the template (kept for backward compatibility)
        pre_checks: JSON string of pre-check configurations
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
            "template",
            "template_id",
            "shop_identifier",
            "enable_international_call",
            "enable_calling",
            "pre_checks",
            "created_at",
            "updated_at"
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16) RETURNING *;
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
        template,
        template_id,
        shop_identifier,
        enable_international_call,
        enable_calling,
        pre_checks,
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


def get_call_execution_config_by_id_query(config_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get call execution config by ID.
    """
    text = f'SELECT * FROM "{CALL_EXECUTION_CONFIG_TABLE}" WHERE "id" = $1;'
    values: List[Any] = [config_id]
    return text, values


def delete_call_execution_config_query(config_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to delete call execution config by ID.
    """
    text = f'DELETE FROM "{CALL_EXECUTION_CONFIG_TABLE}" WHERE "id" = $1 RETURNING *;'
    values: List[Any] = [config_id]
    return text, values


def update_call_execution_config_query(
    merchant_id: str,
    template: str,
    shop_identifier: Optional[str] = None,
    initial_offset: Optional[int] = None,
    retry_offset: Optional[int] = None,
    call_start_time: Optional[time] = None,
    call_end_time: Optional[time] = None,
    max_retry: Optional[int] = None,
    calling_provider: Optional[CallProvider] = None,
    enable_international_call: Optional[bool] = None,
    enable_calling: Optional[bool] = None,
    template_id: Optional[str] = None,
    pre_checks: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to update call execution config record based on merchant_id, template, and shop_identifier.
    Only updates fields that are provided (not None).

    Args:
        template_id: UUID of the template (preferred, for referential integrity)
        template: Name of the template (kept for backward compatibility)
        pre_checks: JSON string of pre-check configurations
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

    if enable_calling is not None:
        updates.append(f'"enable_calling" = ${param_count}')
        values.append(enable_calling)
        param_count += 1

    if template_id is not None:
        updates.append(f'"template_id" = ${param_count}')
        values.append(template_id)
        param_count += 1

    if pre_checks is not None:
        updates.append(f'"pre_checks" = ${param_count}')
        values.append(pre_checks)
        param_count += 1

    # Always update the updated_at timestamp
    updates.append(f'"updated_at" = ${param_count}')
    values.append(datetime.now())
    param_count += 1

    # Build WHERE clause based on merchant_id, template, and shop_identifier
    values.append(merchant_id)
    merchant_id_param = param_count
    param_count += 1

    values.append(template)
    template_param = param_count
    param_count += 1

    if shop_identifier:
        values.append(shop_identifier)
        shop_identifier_param = param_count
        where_clause = f'"merchant_id" = ${merchant_id_param} AND "template" = ${template_param} AND "shop_identifier" = ${shop_identifier_param}'
    else:
        where_clause = f'"merchant_id" = ${merchant_id_param} AND "template" = ${template_param} AND "shop_identifier" IS NULL'

    text = f"""
        UPDATE "{CALL_EXECUTION_CONFIG_TABLE}"
        SET {', '.join(updates)}
        WHERE {where_clause}
        RETURNING *;
    """

    return text, values


def calling_activation_for_merchant_query(
    enable_calling: bool,
    merchant_id: Optional[str] = None,
    shop_identifier: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to toggle enable_calling for configs.
    - If merchant_id is None: All configs across all merchants are updated
    - If merchant_id is provided but shop_identifier is None: All configs for that merchant are updated
    - If both merchant_id and shop_identifier are provided: Only that specific config is updated
    """
    values: List[Any] = [enable_calling, datetime.now()]

    if merchant_id is None:
        # Update all configs across all merchants
        text = f"""
            UPDATE "{CALL_EXECUTION_CONFIG_TABLE}"
            SET "enable_calling" = $1, "updated_at" = $2
            RETURNING *;
        """
    elif shop_identifier:
        # Update specific shop for specific merchant
        text = f"""
            UPDATE "{CALL_EXECUTION_CONFIG_TABLE}"
            SET "enable_calling" = $1, "updated_at" = $2
            WHERE "merchant_id" = $3 AND "shop_identifier" = $4
            RETURNING *;
        """
        values.append(merchant_id)
        values.append(shop_identifier)
    else:
        # Update all configs for specific merchant
        text = f"""
            UPDATE "{CALL_EXECUTION_CONFIG_TABLE}"
            SET "enable_calling" = $1, "updated_at" = $2
            WHERE "merchant_id" = $3
            RETURNING *;
        """
        values.append(merchant_id)

    return text, values


def get_all_merchants_query() -> Tuple[str, List[Any]]:
    """
    Generate query to get all unique merchants (shop_identifiers).

    Returns all unique shop_identifier values from call_execution_config.
    Each shop_identifier represents a distinct merchant in the system.

    Returns:
        Tuple of (query string, empty values list)
    """
    query = f"""
        SELECT DISTINCT shop_identifier
        FROM {CALL_EXECUTION_CONFIG_TABLE}
        WHERE shop_identifier IS NOT NULL
        ORDER BY shop_identifier ASC
    """

    return query, []


def get_merchant_id_by_shop_identifier_from_config_query(
    shop_identifier: str,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get merchant_id for a given shop_identifier from call_execution_config.

    Looks up the parent merchant_id for a shop from call execution config table.

    Args:
        shop_identifier: Shop identifier to look up

    Returns:
        Tuple of (query string, values list)
    """
    query = f"""
        SELECT DISTINCT merchant_id
        FROM {CALL_EXECUTION_CONFIG_TABLE}
        WHERE shop_identifier = $1
        LIMIT 1
    """

    return query, [shop_identifier]

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
    reseller_id: str,
    template: str,
    merchant_id: Optional[str],
    enable_international_call: bool,
    enable_calling: bool = True,
    enable_inbound: bool = True,
    inbound_call_start_time: Optional[time] = None,
    inbound_call_end_time: Optional[time] = None,
    inbound_call_timezone: Optional[str] = None,
    inbound_block_action: Optional[str] = None,
    inbound_redirect_number: Optional[str] = None,
    inbound_block_message: Optional[str] = None,
    enforce_blacklist: bool = True,
    rate_limit_enabled: bool = False,
    rate_limit_max_calls: Optional[int] = None,
    rate_limit_window_seconds: Optional[int] = None,
    rate_limit_whitelist: Optional[str] = None,
    template_id: Optional[str] = None,
    pre_checks: Optional[str] = None,
    telephony_config: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to insert call execution config record.
    Uses ON CONFLICT to upsert based on (merchant_id, template) to prevent duplicates.

    Args:
        template_id: UUID of the template (preferred, for referential integrity)
        template: Name of the template (kept for backward compatibility)
        pre_checks: JSON string of pre-check configurations
        telephony_config: JSON string of telephony provider overrides
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
            "reseller_id",
            "template",
            "template_id",
            "merchant_id",
            "enable_international_call",
            "enable_calling",
            "enable_inbound",
            "inbound_call_start_time",
            "inbound_call_end_time",
            "inbound_call_timezone",
            "inbound_block_action",
            "inbound_redirect_number",
            "inbound_block_message",
            "enforce_blacklist",
            "rate_limit_enabled",
            "rate_limit_max_calls",
            "rate_limit_window_seconds",
            "rate_limit_whitelist",
            "pre_checks",
            "telephony_config",
            "created_at",
            "updated_at"
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29)
        ON CONFLICT (merchant_id, template) DO UPDATE SET
            initial_offset = EXCLUDED.initial_offset,
            retry_offset = EXCLUDED.retry_offset,
            call_start_time = EXCLUDED.call_start_time,
            call_end_time = EXCLUDED.call_end_time,
            max_retry = EXCLUDED.max_retry,
            calling_provider = EXCLUDED.calling_provider,
            template_id = EXCLUDED.template_id,
            enable_international_call = EXCLUDED.enable_international_call,
            enable_calling = EXCLUDED.enable_calling,
            enable_inbound = EXCLUDED.enable_inbound,
            inbound_call_start_time = EXCLUDED.inbound_call_start_time,
            inbound_call_end_time = EXCLUDED.inbound_call_end_time,
            inbound_call_timezone = EXCLUDED.inbound_call_timezone,
            inbound_block_action = EXCLUDED.inbound_block_action,
            inbound_redirect_number = EXCLUDED.inbound_redirect_number,
            inbound_block_message = EXCLUDED.inbound_block_message,
            enforce_blacklist = EXCLUDED.enforce_blacklist,
            rate_limit_enabled = EXCLUDED.rate_limit_enabled,
            rate_limit_max_calls = EXCLUDED.rate_limit_max_calls,
            rate_limit_window_seconds = EXCLUDED.rate_limit_window_seconds,
            rate_limit_whitelist = EXCLUDED.rate_limit_whitelist,
            pre_checks = EXCLUDED.pre_checks,
            telephony_config = EXCLUDED.telephony_config,
            updated_at = EXCLUDED.updated_at
        RETURNING *;
    """

    values = [
        id,
        initial_offset,
        retry_offset,
        call_start_time,
        call_end_time,
        max_retry,
        calling_provider.value,
        reseller_id,
        template,
        template_id,
        merchant_id,
        enable_international_call,
        enable_calling,
        enable_inbound,
        inbound_call_start_time,
        inbound_call_end_time,
        inbound_call_timezone,
        inbound_block_action,
        inbound_redirect_number,
        inbound_block_message,
        enforce_blacklist,
        rate_limit_enabled,
        rate_limit_max_calls,
        rate_limit_window_seconds,
        rate_limit_whitelist,
        pre_checks,
        telephony_config,
        datetime.now(),
        datetime.now(),
    ]

    return text, values


def get_call_execution_config_by_merchant_id_query(
    reseller_id: str,
    merchant_id: Optional[str],
) -> Tuple[str, List[Any]]:
    """
    Generate query to get call execution config by reseller ID and merchant identifier.
    """
    if merchant_id:
        text = f"""
            SELECT *
            FROM "{CALL_EXECUTION_CONFIG_TABLE}" 
            WHERE reseller_id = $1 
            AND merchant_id = $2;
        """
        values: List[Any] = [reseller_id, merchant_id]
    else:
        text = f"""
            SELECT *
            FROM "{CALL_EXECUTION_CONFIG_TABLE}" 
            WHERE reseller_id = $1;
        """
        values = [reseller_id]
    return text, values


def get_all_call_execution_configs_query() -> Tuple[str, List[Any]]:
    """
    Generate query to get all call execution configs.
    """
    text = f"""
        SELECT *
        FROM "{CALL_EXECUTION_CONFIG_TABLE}";
    """
    values: List[Any] = []
    return text, values


def get_call_execution_config_by_id_query(config_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get call execution config by ID.
    """
    text = f"""
        SELECT *
        FROM "{CALL_EXECUTION_CONFIG_TABLE}" WHERE "id" = $1;
    """
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
    reseller_id: str,
    template: str,
    merchant_id: Optional[str] = None,
    initial_offset: Optional[int] = None,
    retry_offset: Optional[int] = None,
    call_start_time: Optional[time] = None,
    call_end_time: Optional[time] = None,
    max_retry: Optional[int] = None,
    calling_provider: Optional[CallProvider] = None,
    enable_international_call: Optional[bool] = None,
    enable_calling: Optional[bool] = None,
    enable_inbound: Optional[bool] = None,
    inbound_call_start_time: Optional[time] = None,
    inbound_call_end_time: Optional[time] = None,
    inbound_call_timezone: Optional[str] = None,
    inbound_block_action: Optional[str] = None,
    inbound_redirect_number: Optional[str] = None,
    inbound_block_message: Optional[str] = None,
    enforce_blacklist: Optional[bool] = None,
    rate_limit_enabled: Optional[bool] = None,
    rate_limit_max_calls: Optional[int] = None,
    rate_limit_window_seconds: Optional[int] = None,
    rate_limit_whitelist: Optional[str] = None,
    template_id: Optional[str] = None,
    pre_checks: Optional[str] = None,
    telephony_config: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to update call execution config record based on reseller_id, template, and merchant_id.
    Only updates fields that are provided (not None).
    """
    updates = []
    values: List[Any] = []
    param_count = 1

    # Helper to append a simple field update
    def _add(col: str, val: Any) -> None:
        nonlocal param_count
        updates.append(f'"{col}" = ${param_count}')
        values.append(val)
        param_count += 1

    if initial_offset is not None:
        _add("initial_offset", initial_offset)
    if retry_offset is not None:
        _add("retry_offset", retry_offset)
    if call_start_time is not None:
        _add("call_start_time", call_start_time)
    if call_end_time is not None:
        _add("call_end_time", call_end_time)
    if max_retry is not None:
        _add("max_retry", max_retry)
    if calling_provider is not None:
        _add("calling_provider", calling_provider.value)
    if enable_international_call is not None:
        _add("enable_international_call", enable_international_call)
    if enable_calling is not None:
        _add("enable_calling", enable_calling)
    if enable_inbound is not None:
        _add("enable_inbound", enable_inbound)
    if inbound_call_start_time is not None:
        _add("inbound_call_start_time", inbound_call_start_time)
    if inbound_call_end_time is not None:
        _add("inbound_call_end_time", inbound_call_end_time)
    if inbound_call_timezone is not None:
        _add("inbound_call_timezone", inbound_call_timezone)
    if inbound_block_action is not None:
        _add("inbound_block_action", inbound_block_action)
    if inbound_redirect_number is not None:
        _add("inbound_redirect_number", inbound_redirect_number)
    if inbound_block_message is not None:
        _add("inbound_block_message", inbound_block_message)
    if enforce_blacklist is not None:
        _add("enforce_blacklist", enforce_blacklist)
    if rate_limit_enabled is not None:
        _add("rate_limit_enabled", rate_limit_enabled)
    if rate_limit_max_calls is not None:
        _add("rate_limit_max_calls", rate_limit_max_calls)
    if rate_limit_window_seconds is not None:
        _add("rate_limit_window_seconds", rate_limit_window_seconds)
    if rate_limit_whitelist is not None:
        _add("rate_limit_whitelist", rate_limit_whitelist)
    if template_id is not None:
        _add("template_id", template_id)

    if pre_checks is not None:
        updates.append(f'"pre_checks" = ${param_count}::json')
        values.append(pre_checks)
        param_count += 1

    if telephony_config is not None:
        if telephony_config == "__CLEAR__":
            updates.append('"telephony_config" = NULL')
        else:
            updates.append(f'"telephony_config" = ${param_count}::jsonb')
            values.append(telephony_config)
            param_count += 1

    # Always update the updated_at timestamp
    updates.append(f'"updated_at" = ${param_count}')
    values.append(datetime.now())
    param_count += 1

    # Build WHERE clause based on reseller_id, template, and merchant_id
    values.append(reseller_id)
    reseller_id_param = param_count
    param_count += 1

    values.append(template)
    template_param = param_count
    param_count += 1

    if merchant_id:
        values.append(merchant_id)
        merchant_identifier_param = param_count
        where_clause = f'reseller_id = ${reseller_id_param} AND "template" = ${template_param} AND merchant_id = ${merchant_identifier_param}'
    else:
        where_clause = f'reseller_id = ${reseller_id_param} AND "template" = ${template_param} AND merchant_id IS NULL'

    text = f"""
        UPDATE "{CALL_EXECUTION_CONFIG_TABLE}"
        SET {", ".join(updates)}
        WHERE {where_clause}
        RETURNING *;
    """

    return text, values


def calling_activation_for_merchant_query(
    enable_calling: bool,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to toggle enable_calling for configs.
    - If reseller_id is None: All configs across all resellers are updated
    - If reseller_id is provided but merchant_id is None: All configs for that reseller are updated
    - If both reseller_id and merchant_id are provided: Only that specific config is updated
    """
    values: List[Any] = [enable_calling, datetime.now()]

    if reseller_id is None:
        # Update all configs across all resellers
        text = f"""
            UPDATE "{CALL_EXECUTION_CONFIG_TABLE}"
            SET "enable_calling" = $1, "updated_at" = $2
            RETURNING *;
        """
    elif merchant_id:
        # Update specific merchant for specific reseller
        text = f"""
            UPDATE "{CALL_EXECUTION_CONFIG_TABLE}"
            SET "enable_calling" = $1, "updated_at" = $2
            WHERE reseller_id = $3 
            AND merchant_id = $4
            RETURNING *;
        """
        values.append(reseller_id)
        values.append(merchant_id)
    else:
        # Update all configs for specific reseller
        text = f"""
            UPDATE "{CALL_EXECUTION_CONFIG_TABLE}"
            SET "enable_calling" = $1, "updated_at" = $2
            WHERE reseller_id = $3
            RETURNING *;
        """
        values.append(reseller_id)

    return text, values


def get_all_merchants_query() -> Tuple[str, List[Any]]:
    """
    Generate query to get all unique resellers (merchant_ids).

    Returns all unique merchant_id values from call_execution_config.
    Each merchant_id represents a distinct reseller in the system.

    Returns:
        Tuple of (query string, empty values list)
    """
    query = f"""
        SELECT DISTINCT merchant_id
        FROM {CALL_EXECUTION_CONFIG_TABLE}
        WHERE merchant_id IS NOT NULL
        ORDER BY merchant_id ASC
    """

    return query, []


def get_reseller_id_by_merchant_identifier_from_config_query(
    merchant_id: str,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get reseller_id for a given merchant_id from call_execution_config.

    Looks up the parent reseller_id for a merchant from call execution config table.

    Args:
        merchant_id: merchant identifier to look up

    Returns:
        Tuple of (query string, values list)
    """
    query = f"""
        SELECT DISTINCT reseller_id
        FROM {CALL_EXECUTION_CONFIG_TABLE}
        WHERE merchant_id = $1
        LIMIT 1
    """

    return query, [merchant_id]

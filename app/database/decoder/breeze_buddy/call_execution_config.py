"""
Decoder functions for call execution config.
"""

import json
from typing import Any, List, Optional

import asyncpg

from app.core.logger import logger
from app.schemas import (
    CallExecutionConfig,
    CallProvider,
    InboundBlockAction,
    PreCheckConfig,
    TelephonyConfig,
)
from app.utils.common import parse_json


def _decode_pre_checks(raw: Any) -> Optional[List[PreCheckConfig]]:
    """
    Decode the pre_checks JSONB column into a list of PreCheckConfig.
    Handles both JSON string and already-parsed list from asyncpg.
    """
    if raw is None:
        return None

    # Handle case where asyncpg returns string instead of parsed JSON
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse pre_checks JSON string: {e}")
            return None

    if not isinstance(data, list):
        logger.warning(
            f"pre_checks column has unexpected type {type(data).__name__}, expected list. "
            "Pre-checks will be skipped for this config."
        )
        return None

    # Parse each pre-check config, skipping invalid ones
    pre_checks: List[PreCheckConfig] = []
    for item in data:
        try:
            pre_checks.append(PreCheckConfig(**item))
        except Exception as e:
            logger.warning(f"Skipping malformed pre_check config: {e}")

    return pre_checks if pre_checks else None


def _decode_single_row(row: asyncpg.Record) -> CallExecutionConfig:
    """Decode a single call execution config row."""
    return CallExecutionConfig(
        id=row["id"],
        initial_offset=row["initial_offset"],
        retry_offset=row["retry_offset"],
        call_start_time=row["call_start_time"],
        call_end_time=row["call_end_time"],
        max_retry=row["max_retry"],
        calling_provider=CallProvider(row["calling_provider"]),
        reseller_id=row["merchant_id"] or row["reseller_id"],
        merchant_id=row["merchant_id"] or row["reseller_id"],  # Backward compatibility
        template=row["template"],
        merchant_identifier=row["shop_identifier"] or row["merchant_identifier"],
        shop_identifier=row["shop_identifier"] or row["merchant_identifier"],
        enable_international_call=row["enable_international_call"],
        enable_calling=row["enable_calling"],
        enable_inbound=row.get("enable_inbound", True),
        inbound_call_start_time=row.get("inbound_call_start_time"),
        inbound_call_end_time=row.get("inbound_call_end_time"),
        inbound_block_action=(
            InboundBlockAction(row["inbound_block_action"])
            if row.get("inbound_block_action")
            else InboundBlockAction.REJECT
        ),
        inbound_redirect_number=row.get("inbound_redirect_number"),
        inbound_block_message=row.get("inbound_block_message"),
        pre_checks=_decode_pre_checks(row.get("pre_checks")),
        telephony_config=(
            TelephonyConfig(**parsed)
            if (parsed := parse_json(row, "telephony_config"))
            else None
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_call_execution_config_list(
    result: List[asyncpg.Record],
) -> List[CallExecutionConfig]:
    """
    Decode multiple call execution config records from database result using Pydantic models.
    """
    if not result:
        return []

    return [_decode_single_row(row) for row in result]


def decode_call_execution_config(
    result: List[asyncpg.Record],
) -> Optional[CallExecutionConfig]:
    """
    Decode call execution config from database result using Pydantic model.
    """
    if not result or len(result) == 0:
        return None

    return _decode_single_row(result[0])

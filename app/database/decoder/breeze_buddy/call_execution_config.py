"""
Decoder functions for call execution config.
"""

import json
from typing import Any, List, Optional

import asyncpg

from app.schemas import CallExecutionConfig, CallProvider, PreCheckConfig


def _decode_pre_checks(raw: Any) -> Optional[List[PreCheckConfig]]:
    """
    Decode the pre_checks JSONB column into a list of PreCheckConfig.
    Handles both JSON string and already-parsed list/dict from asyncpg.
    """
    if raw is None:
        return None

    # asyncpg returns JSONB as already-parsed Python objects
    data = raw
    if isinstance(raw, str):
        data = json.loads(raw)

    if not isinstance(data, list):
        return None

    return [PreCheckConfig(**item) for item in data]


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
        merchant_id=row["merchant_id"],
        template=row["template"],
        shop_identifier=row["shop_identifier"],
        enable_international_call=row["enable_international_call"],
        enable_calling=row["enable_calling"],
        pre_checks=_decode_pre_checks(row.get("pre_checks")),
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

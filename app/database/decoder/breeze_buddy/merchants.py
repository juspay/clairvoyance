"""
Merchant entity accessors - response builders for merchant entity queries.
"""

import json
from typing import Any, List, Optional

from app.schemas.breeze_buddy.merchants import (
    CALL_LIMIT_MAX_RULES,
    AnalyticsConfigResponse,
    AnalyticsFieldConfig,
    CallLimit,
    CallLimitsResponse,
    MerchantResponse,
)


def decode_merchant(row) -> MerchantResponse:
    """Build MerchantResponse from database row.

    Args:
        row: Database row containing merchant entity data

    Returns:
        MerchantResponse instance
    """
    return MerchantResponse(
        merchant_id=row["merchant_id"],
        name=row.get("name"),
        description=row.get("description"),
        is_active=row.get("is_active", True),
        reseller_id=str(row["reseller_id"]) if row.get("reseller_id") else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_call_limits(raw: Any) -> Optional[List[CallLimit]]:
    """The stored ``merchants.call_limits`` value as rules, or None for no rule.

    STRICT: a value that is not a list of valid rules RAISES (ValueError or
    pydantic's ValidationError) instead of reading as "no rule". The dispatch
    worker enforces this at the dial, and a row it cannot understand must stop
    the call (fail closed), never wave it through. The column carries no
    codec, so jsonb arrives as text.
    """
    if raw is None:
        return None
    value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    if not isinstance(value, list):
        raise ValueError(f"call_limits must be a list, got {type(value).__name__}")
    if len(value) > CALL_LIMIT_MAX_RULES:
        raise ValueError(
            f"call_limits holds {len(value)} rules; at most "
            f"{CALL_LIMIT_MAX_RULES} is supported"
        )
    rules = [CallLimit.model_validate(item) for item in value]
    return rules or None


def decode_call_limits_row(row) -> CallLimitsResponse:
    """Build CallLimitsResponse from a ``merchant_id, call_limits`` row."""
    return CallLimitsResponse(
        merchant_id=row["merchant_id"],
        call_limits=decode_call_limits(row["call_limits"]),
    )


def decode_analytics_field_config(raw: Any) -> Optional[AnalyticsFieldConfig]:
    """The stored ``merchants.analytics_field_config``, or None for none.

    STRICT: a value the schema refuses raises; read as empty, every mapped
    column would silently go NULL. jsonb arrives as text (no codec).
    """
    if raw is None:
        return None
    value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    config = AnalyticsFieldConfig.model_validate(value)
    return None if config.is_empty() else config


def decode_analytics_config_row(row) -> AnalyticsConfigResponse:
    """Build AnalyticsConfigResponse from a merchant row's config columns."""
    return AnalyticsConfigResponse(
        merchant_id=row["merchant_id"],
        analytics_field_config=decode_analytics_field_config(
            row["analytics_field_config"]
        ),
    )

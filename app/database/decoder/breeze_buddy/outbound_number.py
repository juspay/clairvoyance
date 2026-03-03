"""
Decoder functions for outbound number.
"""

import json
from typing import List, Optional

import asyncpg

from app.schemas import CallProvider, IvrVoiceConfig, OutboundNumber, OutboundNumberStatus


def _decode_ivr_config(raw: object) -> Optional[IvrVoiceConfig]:
    """Parse the ivr_config column (JSONB) into an IvrVoiceConfig model."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = json.loads(raw)
    return IvrVoiceConfig(**raw)


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
        merchant_id=row["merchant_id"],
        shop_identifier=row["shop_identifier"],
        ivr_config=_decode_ivr_config(row.get("ivr_config")),
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
            merchant_id=row["merchant_id"],
            shop_identifier=row["shop_identifier"],
            ivr_config=_decode_ivr_config(row.get("ivr_config")),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in result
    ]

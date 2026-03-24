"""
Decoder functions for outbound number.
"""

from typing import List, Optional

import asyncpg

from app.schemas import CallProvider, OutboundNumber, OutboundNumberStatus


def _decode_single_row(row: asyncpg.Record) -> OutboundNumber:
    """Decode a single asyncpg row into an OutboundNumber model."""
    return OutboundNumber(
        id=row["id"],
        number=row["number"],
        provider=CallProvider(row["provider"]),
        status=OutboundNumberStatus(row["status"]),
        channels=row["channels"],
        maximum_channels=row["maximum_channels"],
        reseller_id=row["reseller_id"],
        merchant_id=row["merchant_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_outbound_number(result: List[asyncpg.Record]) -> Optional[OutboundNumber]:
    """
    Decode outbound number from database result using Pydantic model.
    """
    if not result or len(result) == 0:
        return None

    return _decode_single_row(result[0])


def decode_outbound_number_list(result: List[asyncpg.Record]) -> List[OutboundNumber]:
    """
    Decode multiple outbound number records from database result using Pydantic models.
    """
    if not result:
        return []

    return [_decode_single_row(row) for row in result]

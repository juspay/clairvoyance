"""
Decoder functions for outbound number pool.
"""

from typing import List, Optional

import asyncpg

from app.schemas import CallProvider, OutboundNumberPool, OutboundNumberPoolStatus


def decode_outbound_number_pool(
    result: List[asyncpg.Record],
) -> Optional[OutboundNumberPool]:
    """
    Decode a single outbound number pool from database result.
    """
    if not result or len(result) == 0:
        return None

    row = result[0]

    return OutboundNumberPool(
        id=row["id"],
        name=row["name"],
        provider=CallProvider(row["provider"]),
        reseller_id=row["reseller_id"],
        merchant_id=row["merchant_id"],
        max_channels=row["max_channels"],
        current_channels=row["current_channels"],
        rotation_index=row["rotation_index"],
        status=OutboundNumberPoolStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_outbound_number_pool_list(
    result: List[asyncpg.Record],
) -> List[OutboundNumberPool]:
    """
    Decode multiple outbound number pool records from database result.
    """
    if not result:
        return []

    return [
        OutboundNumberPool(
            id=row["id"],
            name=row["name"],
            provider=CallProvider(row["provider"]),
            reseller_id=row["reseller_id"],
            merchant_id=row["merchant_id"],
            max_channels=row["max_channels"],
            current_channels=row["current_channels"],
            rotation_index=row["rotation_index"],
            status=OutboundNumberPoolStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in result
    ]

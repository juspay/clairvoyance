"""
Decoder functions for outbound number.
"""

from typing import List, Optional

import asyncpg

from app.schemas import CallProvider, TelephonyNumber, TelephonyNumberStatus


def decode_telephony_number(result: List[asyncpg.Record]) -> Optional[TelephonyNumber]:
    """
    Decode outbound number from database result using Pydantic model.
    """
    if not result or len(result) == 0:
        return None

    row = result[0]

    return TelephonyNumber(
        id=row["id"],
        number=row["number"],
        provider=CallProvider(row["provider"]),
        status=TelephonyNumberStatus(row["status"]),
        channels=row["channels"],
        maximum_channels=row["maximum_channels"],
        reseller_id=row["reseller_id"],
        merchant_id=row["merchant_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_telephony_number_list(result: List[asyncpg.Record]) -> List[TelephonyNumber]:
    """
    Decode multiple outbound number records from database result using Pydantic models.
    """
    if not result:
        return []

    result[0]

    return [
        TelephonyNumber(
            id=row["id"],
            number=row["number"],
            provider=CallProvider(row["provider"]),
            status=TelephonyNumberStatus(row["status"]),
            channels=row["channels"],
            maximum_channels=row["maximum_channels"],
            reseller_id=row["reseller_id"],
            merchant_id=row["merchant_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in result
    ]

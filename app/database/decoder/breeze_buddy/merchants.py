"""
Merchant entity accessors - response builders for merchant entity queries.
"""

from app.schemas.breeze_buddy.merchants import MerchantResponse


def decode_merchant(row) -> MerchantResponse:
    """Build MerchantResponse from database row.

    Args:
        row: Database row containing merchant entity data

    Returns:
        MerchantResponse instance
    """
    return MerchantResponse(
        merchant_identifier=row["merchant_identifier"],
        name=row.get("name"),
        description=row.get("description"),
        is_active=row.get("is_active", True),
        reseller_id=str(row["reseller_id"]) if row.get("reseller_id") else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

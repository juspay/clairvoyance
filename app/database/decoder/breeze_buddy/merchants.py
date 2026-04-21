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
        merchant_id=row["merchant_id"],
        name=row.get("name"),
        description=row.get("description"),
        is_active=row.get("is_active", True),
        reseller_id=str(row["reseller_id"]) if row.get("reseller_id") else None,
        pickup_rate_alert_enabled=row.get("pickup_rate_alert_enabled", False),
        pickup_rate_alert_threshold=row.get("pickup_rate_alert_threshold"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

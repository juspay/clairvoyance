"""Shared helpers for data source template attachment."""

from typing import Optional

from app.schemas.breeze_buddy.data_source import DataSourceResponse

DATA_SOURCE_UNAVAILABLE = "[Data unavailable]"


def data_source_in_template_scope(
    data_source: DataSourceResponse,
    reseller_id: str,
    merchant_id: Optional[str],
) -> bool:
    """Return whether a data source can be attached to a template scope."""
    if not data_source.is_active or data_source.reseller_id != reseller_id:
        return False
    return data_source.merchant_id is None or data_source.merchant_id == merchant_id

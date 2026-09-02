"""crm_channel_binding rows -> domain shapes."""

from typing import Any, Mapping

from app.crm.connectivity.schemas import ChannelBinding
from app.crm.shared.decode import jsonb_object


def decode_binding(row: Mapping[str, Any]) -> ChannelBinding:
    """One crm_channel_binding row -> ChannelBinding."""
    return ChannelBinding(
        id=str(row["id"]),
        merchant_id=row["merchant_id"],
        channel=row["channel"],
        installation_id=str(row["installation_id"]),
        address=row["address"],
        # An unreadable capabilities blob must not refuse a send: an empty
        # dict means "declares nothing", which every caller already handles.
        capabilities=jsonb_object(row["capabilities"]),
        is_primary=row["is_primary"],
        status=row["status"],
    )

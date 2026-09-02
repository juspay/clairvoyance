"""crm_message rows -> domain shapes. Never imported outside this db package."""

from typing import Any, Mapping

from app.crm.connectivity.schemas import QueuedMessage
from app.crm.shared.decode import jsonb_object, uuid_or_none


def decode_queued_message(row: Mapping[str, Any]) -> QueuedMessage:
    """One claimed crm_message row -> QueuedMessage."""
    return QueuedMessage(
        id=str(row["id"]),
        merchant_id=row["merchant_id"],
        customer_id=str(row["customer_id"]),
        channel=row["channel"],
        sent_to_address=row["sent_to_address"],
        binding_id=uuid_or_none(row["binding_id"]),
        source_kind=row["source_kind"],
        source_id=uuid_or_none(row["source_id"]),
        purpose_key=row["purpose_key"],
        template_id=row["template_id"],
        # Totality matters most here: a whole batch is decoded after its claim
        # commits but outside the per-message error handling, so one raise
        # would strand every row in it — permanently (see shared/decode.py).
        variables=jsonb_object(row["variables"]),
        dedupe_key=row["dedupe_key"],
        attempt=row["attempt"],
        next_attempt_at=row["next_attempt_at"],
    )

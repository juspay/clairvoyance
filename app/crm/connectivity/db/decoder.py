"""Database rows -> domain shapes. Never imported outside this db package."""

import json
from typing import Any, Dict, Mapping

from app.crm.connectivity.schemas import QueuedMessage


def _load_variables(value: Any) -> Dict[str, Any]:
    """Parse the stored variables blob. Total: always a dict, never raises.

    A batch is decoded after the claim commits but outside the per-message
    error handling, so raising here strands every row in the batch — they get
    reclaimed, decoded, and raise again, forever. One bad row would stall the
    queue permanently.

    The column is plain jsonb, so 42 or [1, 2] are legal values. Non-dicts are
    dropped rather than converted, since dict() would turn [["a", 1]] into
    {"a": 1} and invent a variable. The driver returns jsonb as a string
    today; the non-string branch covers a codec being registered later.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _uuid_or_none(value: Any) -> Any:
    return str(value) if value is not None else None


def decode_queued_message(row: Mapping[str, Any]) -> QueuedMessage:
    return QueuedMessage(
        id=str(row["id"]),
        merchant_id=row["merchant_id"],
        customer_id=str(row["customer_id"]),
        channel=row["channel"],
        sent_to_address=row["sent_to_address"],
        binding_id=_uuid_or_none(row["binding_id"]),
        source_kind=row["source_kind"],
        source_id=_uuid_or_none(row["source_id"]),
        purpose_key=row["purpose_key"],
        template_id=row["template_id"],
        variables=_load_variables(row["variables"]),
        dedupe_key=row["dedupe_key"],
        attempt=row["attempt"],
        next_attempt_at=row["next_attempt_at"],
    )

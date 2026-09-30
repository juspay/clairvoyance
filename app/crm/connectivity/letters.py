"""The letters this module files on the event spine about its OWN changes.

One today: ``binding.buddy_moved`` — Buddy now answers on another binding
(settings.py files it once the move commits).

Fire-and-forget through record's ``record_event``: filing a fact must never
break the change it records. A letter that failed to file is logged by
record; the change itself is unaffected.
"""

import uuid
from typing import Optional

from app.crm.connectivity.topics import TOPIC_BUDDY_MOVED
from app.crm.record.contracts import record_event


async def file_buddy_moved_letter(
    *,
    merchant_id: str,
    channel: str,
    from_binding_id: str,
    to_binding_id: str,
) -> Optional[str]:
    """File ``binding.buddy_moved``: Buddy now answers on ``to_binding_id``
    instead of ``from_binding_id``. Merchant-level (no customer). Each move
    is its own fact, so its external id is fresh — two moves back and forth
    are two letters, never a duplicate."""
    return await record_event(
        merchant_id=merchant_id,
        source=channel,
        topic=TOPIC_BUDDY_MOVED,
        external_id=f"buddy-moved:{uuid.uuid4()}",
        payload={
            "channel": channel,
            "from_binding_id": from_binding_id,
            "to_binding_id": to_binding_id,
        },
    )

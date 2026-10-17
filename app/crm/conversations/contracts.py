"""Conversations' public surface — the only file other modules import.

- ``consume_conversation_event`` / ``consume_buddy_moved`` — the projector
  and the "Buddy moved" consumer; worker_main registers both in record's
  consumer slot.
"""

from app.crm.conversations.moves import consume_buddy_moved
from app.crm.conversations.project import consume_conversation_event

__all__ = [
    # worker_main registers these
    "consume_conversation_event",
    "consume_buddy_moved",
]

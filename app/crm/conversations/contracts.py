"""Conversations' public surface — the only file other modules import.

- ``consume_conversation_event`` / ``consume_buddy_bot_moved`` — the projector
  and the "Buddy moved" consumer; worker_main registers both in record's
  consumer slot.
- ``add_template_to_thread`` — worker_main registers it in connectivity's
  template hook: a queued template on Buddy's binding joins her thread.
"""

from app.crm.conversations.moves import consume_buddy_bot_moved
from app.crm.conversations.project import (
    add_template_to_thread,
    consume_conversation_event,
)

__all__ = [
    # worker_main registers these
    "consume_conversation_event",
    "consume_buddy_bot_moved",
    # worker_main registers this in connectivity's template hook
    "add_template_to_thread",
]

"""Conversations' public surface — the only file other modules import.

- ``consume_conversation_event`` / ``consume_buddy_bot_moved`` — the projector
  and the "Buddy moved" consumer; worker_main registers both in record's
  consumer slot.
- ``add_template_to_thread`` — worker_main registers it in connectivity's
  template hook: a queued template on Buddy's binding joins her thread.
- ``claim_inbox_tick`` / ``run_inbox_tick`` — the sweeps' drain loop, which
  worker_main hosts on the walker role.
- Buddy's turns (PR 3, buddy-side, on the API pods): ``get_bot_work``,
  ``can_bot_speak``, ``update_bot_session``, ``get_unanswered_messages``,
  ``get_teammate_era_messages``, ``record_bot_reply``, ``advance_bot_cursor``,
  ``get_thread_by_id``, ``get_threads_by_ids``, ``can_request_handoff``,
  ``request_handoff``, and the words they read rows and send by.
"""

from app.crm.conversations.bot import (
    advance_bot_cursor,
    can_bot_speak,
    get_bot_work,
    get_teammate_era_messages,
    get_thread_by_id,
    get_threads_by_ids,
    get_unanswered_messages,
    record_bot_reply,
    update_bot_session,
)
from app.crm.conversations.handoffs import can_request_handoff, request_handoff
from app.crm.conversations.moves import consume_buddy_bot_moved
from app.crm.conversations.project import (
    add_template_to_thread,
    consume_conversation_event,
)
from app.crm.conversations.schemas import BotWork, Thread, TimelineRow
from app.crm.conversations.status import (
    HANDOFF_PRIORITIES,
    KIND_INBOUND,
    SERVICE_PURPOSE,
    SOURCE_AGENT,
)
from app.crm.conversations.workers import claim_inbox_tick, run_inbox_tick

__all__ = [
    # worker_main registers these
    "consume_conversation_event",
    "consume_buddy_bot_moved",
    # worker_main registers this in connectivity's template hook
    "add_template_to_thread",
    "claim_inbox_tick",
    "run_inbox_tick",
    # Buddy's turns
    "get_bot_work",
    "can_bot_speak",
    "update_bot_session",
    "get_unanswered_messages",
    "get_teammate_era_messages",
    "record_bot_reply",
    "advance_bot_cursor",
    "get_thread_by_id",
    "get_threads_by_ids",
    "can_request_handoff",
    "request_handoff",
    # the words a caller reads rows and threads by, and sends under
    "KIND_INBOUND",
    "SERVICE_PURPOSE",
    "SOURCE_AGENT",
    "HANDOFF_PRIORITIES",
    # shapes
    "BotWork",
    "Thread",
    "TimelineRow",
]

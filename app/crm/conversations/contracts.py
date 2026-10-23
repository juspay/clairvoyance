"""Conversations' public surface — the only file other modules import.

- ``consume_conversation_event`` / ``consume_buddy_moved`` — the projector
  and the "Buddy moved" consumer; worker_main registers both in record's
  consumer slot.
- ``claim_inbox_tick`` / ``run_inbox_tick`` — the sweeps' drain loop, which
  worker_main hosts on the walker role.
- Buddy's turns (PR 3, buddy-side, on the API pods): ``bot_work``,
  ``bot_may_speak``, ``set_bot_session``, ``pending_inbound``,
  ``human_era_slice``, ``record_bot_reply``, ``mark_bot_cursor``,
  ``thread_by_id``, ``threads_by_ids``, ``handoff_available``,
  ``request_handoff``, and the words they read rows and send by.
"""

from app.crm.conversations.bot import (
    bot_may_speak,
    bot_work,
    human_era_slice,
    mark_bot_cursor,
    pending_inbound,
    record_bot_reply,
    set_bot_session,
    thread_by_id,
    threads_by_ids,
)
from app.crm.conversations.handoffs import handoff_available, request_handoff
from app.crm.conversations.moves import consume_buddy_moved
from app.crm.conversations.project import consume_conversation_event
from app.crm.conversations.schemas import BotWork, Thread, TimelineRow
from app.crm.conversations.status import (
    HANDOFF_PRIORITIES,
    KIND_INBOUND,
    RESUME_CLAIM_TIMEOUT,
    RESUME_HANDED_BACK,
    RESUME_REASONS,
    SERVICE_PURPOSE,
    SOURCE_AGENT,
)
from app.crm.conversations.workers import claim_inbox_tick, run_inbox_tick

__all__ = [
    # worker_main registers these
    "consume_conversation_event",
    "consume_buddy_moved",
    "claim_inbox_tick",
    "run_inbox_tick",
    # Buddy's turns
    "bot_work",
    "bot_may_speak",
    "set_bot_session",
    "pending_inbound",
    "human_era_slice",
    "record_bot_reply",
    "mark_bot_cursor",
    "thread_by_id",
    "threads_by_ids",
    "handoff_available",
    "request_handoff",
    # the words a caller reads rows and threads by, and sends under
    "KIND_INBOUND",
    "SERVICE_PURPOSE",
    "SOURCE_AGENT",
    "HANDOFF_PRIORITIES",
    # why Buddy has a thread back (the answer route's ``reason``)
    "RESUME_HANDED_BACK",
    "RESUME_CLAIM_TIMEOUT",
    "RESUME_REASONS",
    # shapes
    "BotWork",
    "Thread",
    "TimelineRow",
]

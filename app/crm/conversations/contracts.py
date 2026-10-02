"""Conversations' public surface — the only file other modules import.

- ``consume_conversation_event`` / ``consume_buddy_moved`` — the projector
  and the "Buddy moved" consumer; worker_main registers both in record's
  consumer slot.
- ``claim_inbox_tick`` / ``run_inbox_tick`` — the sweeps' drain loop, which
  worker_main hosts on the walker role.
- Buddy's responder (PR 3, buddy-side): ``claim_bot_work``,
  ``bot_may_speak``, ``set_bot_session``, ``thread_for_session``,
  ``pending_inbound``, ``human_era_slice``, ``record_bot_reply``,
  ``mark_bot_cursor``, ``thread_by_id``, ``handoff_available``,
  ``request_handoff``.
- The widget (PR 3): ``open_widget_thread``, ``append_widget_inbound``,
  ``widget_messages_after``.
"""

from app.crm.conversations.bot import (
    bot_may_speak,
    claim_bot_work,
    human_era_slice,
    mark_bot_cursor,
    pending_inbound,
    record_bot_reply,
    set_bot_session,
    thread_by_id,
    thread_for_session,
)
from app.crm.conversations.handoffs import handoff_available, request_handoff
from app.crm.conversations.moves import consume_buddy_moved
from app.crm.conversations.project import consume_conversation_event
from app.crm.conversations.reply import SERVICE_PURPOSE, SOURCE_AGENT
from app.crm.conversations.schemas import BotWork, Handoff, Thread, TimelineRow
from app.crm.conversations.status import CHANNEL_WHATSAPP, KIND_INBOUND
from app.crm.conversations.widget import (
    append_widget_inbound,
    open_widget_thread,
    widget_messages_after,
)
from app.crm.conversations.workers import claim_inbox_tick, run_inbox_tick

__all__ = [
    # worker_main registers these
    "consume_conversation_event",
    "consume_buddy_moved",
    "claim_inbox_tick",
    "run_inbox_tick",
    # Buddy's responder
    "claim_bot_work",
    "bot_may_speak",
    "set_bot_session",
    "thread_for_session",
    "pending_inbound",
    "human_era_slice",
    "record_bot_reply",
    "mark_bot_cursor",
    "thread_by_id",
    "handoff_available",
    "request_handoff",
    # the widget
    "open_widget_thread",
    "append_widget_inbound",
    "widget_messages_after",
    # the words a caller reads rows and threads by, and sends under
    "CHANNEL_WHATSAPP",
    "KIND_INBOUND",
    "SERVICE_PURPOSE",
    "SOURCE_AGENT",
    # shapes
    "BotWork",
    "Handoff",
    "Thread",
    "TimelineRow",
]

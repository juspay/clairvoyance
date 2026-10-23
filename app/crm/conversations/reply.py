"""A teammate's reply (R2): only the teammate holding the thread replies,
and only on a binding with human handoff on (D15).

    messaging channel, inside     free-form text, sent NOW through
    the window                    connectivity's send_session from the
                                  binding she wrote to (the gate and the
                                  send door apply as for any send)
    messaging channel, outside    an approved template, queued from that
    the window                    same binding, on a channel that registers
                                  templates (the provider allows nothing
                                  else); none on a channel without them
    widget                        a timeline row and a wake-up: the widget's
                                  stream delivers it (no manifest, no
                                  provider)

The timeline row is written by THIS call, at once (read-your-writes); the
projector's later pass over the same send is a no-op.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.crm.connectivity.contracts import (
    TextBody,
    queue_message,
    registers_templates_for,
    send_session,
)
from app.crm.conversations.access import Actor
from app.crm.conversations.db.accessors import (
    message as message_accessor,
    thread as thread_accessor,
)
from app.crm.conversations.errors import ConversationError, NotAllowed, ThreadConflict
from app.crm.conversations.realtime import wake
from app.crm.conversations.schemas import Thread, TimelineRow
from app.crm.conversations.status import (
    AUTHOR_TEAMMATE,
    CHANNEL_WIDGET,
    KIND_OUTBOUND,
    SERVICE_PURPOSE,
    SOURCE_HUMAN,
    TEMPLATE_PURPOSE,
    WAKE_MESSAGE,
)
from app.crm.conversations.threads import settings_for, thread_or_404, window_of


@dataclass(frozen=True)
class ReplyResult:
    row: TimelineRow
    #: The manifest's word for the send (accepted · failed · blocked ·
    #: queued), None for the widget, which has no manifest.
    status: Optional[str] = None
    reason: Optional[str] = None


def _takes_templates(thread: Thread) -> bool:
    """PURE: whether a teammate may send this thread a template — only on a
    channel that registers them (the widget has none)."""
    return thread.channel != CHANNEL_WIDGET and registers_templates_for(thread.channel)


async def _holder(merchant_id: str, thread_id: str, actor: Actor) -> Thread:
    if actor.read_only:
        raise NotAllowed("this session is read-only — sign in as a teammate to reply")
    thread = await thread_or_404(merchant_id, thread_id)
    if thread.resolved_at is not None:
        raise ThreadConflict("this conversation is resolved")
    if thread.assignee_user_id != actor.user_id:
        raise NotAllowed("take over the conversation to reply")
    if not (await settings_for(thread)).human_handoff:
        raise NotAllowed("human handoff is off for this connection")
    return thread


async def _record(
    thread: Thread,
    actor: Actor,
    message_id: Optional[str],
    provider_message_id: Optional[str],
    body: Dict[str, Any],
    preview: str,
) -> TimelineRow:
    now = datetime.now(timezone.utc)
    row = await message_accessor.insert_outbound(
        None,
        thread.merchant_id,
        thread.id,
        KIND_OUTBOUND,
        AUTHOR_TEAMMATE,
        actor.user_id,
        message_id,
        provider_message_id,
        body,
        now,
    )
    if row is None:
        raise RuntimeError(f"reply on thread {thread.id} was already recorded")
    await thread_accessor.touch_outbound(thread.merchant_id, thread.id, now, preview)
    await wake(thread.merchant_id, thread.id, WAKE_MESSAGE)
    return row


async def reply_text(
    merchant_id: str, thread_id: str, text: str, actor: Actor
) -> ReplyResult:
    thread = await _holder(merchant_id, thread_id, actor)
    body = {"type": "text", "text": text}
    if thread.channel == CHANNEL_WIDGET:
        return ReplyResult(row=await _record(thread, actor, None, None, body, text))
    if not window_of(thread, datetime.now(timezone.utc)).open:
        raise ThreadConflict(
            "the reply window is closed — send a template instead"
            if _takes_templates(thread)
            else "the reply window is closed — it opens again when they write"
        )
    if thread.customer_id is None or thread.address is None:
        raise ThreadConflict("this conversation has no reply address")
    try:
        result = await send_session(
            merchant_id=merchant_id,
            customer_id=thread.customer_id,
            channel=thread.channel,
            address=thread.address,
            body=TextBody(text=text),
            source_kind=SOURCE_HUMAN,
            source_id=None,
            purpose_key=SERVICE_PURPOSE,
            dedupe_key=f"inbox:{uuid.uuid4()}",
            binding_id=thread.binding_id,
        )
    except ValueError as e:
        # The send door refused the proposal itself (an address it cannot
        # use, a body that does not fit): written for the person, a 400.
        raise ConversationError(str(e)) from e
    row = await _record(
        thread, actor, result.message_id, result.provider_message_id, body, text
    )
    return ReplyResult(row=row, status=result.status, reason=result.reason)


async def reply_template(
    merchant_id: str,
    thread_id: str,
    template_id: str,
    variables: Dict[str, Any],
    actor: Actor,
) -> ReplyResult:
    thread = await _holder(merchant_id, thread_id, actor)
    if not _takes_templates(thread):
        raise ThreadConflict("this channel has no templates — reply with text")
    if thread.customer_id is None or thread.address is None:
        raise ThreadConflict("this conversation has no reply address")
    try:
        message_id = await queue_message(
            merchant_id=merchant_id,
            customer_id=thread.customer_id,
            channel=thread.channel,
            address=thread.address,
            source_kind=SOURCE_HUMAN,
            source_id=None,
            purpose_key=TEMPLATE_PURPOSE,
            template_id=template_id,
            variables=variables,
            dedupe_key=f"inbox:{uuid.uuid4()}",
            binding_id=thread.binding_id,
        )
    except ValueError as e:
        raise ConversationError(str(e)) from e
    body = {"type": "template", "template_id": template_id}
    row = await _record(
        thread, actor, message_id, None, body, f"Template · {template_id}"
    )
    return ReplyResult(row=row, status="queued")

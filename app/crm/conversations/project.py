"""The projector: spine letters -> threads and their timelines, on Buddy's
binding only (R1), on every channel that carries a conversation.
Registered in worker_main through record's consumer slot.

    message.inbound   a customer wrote. On Buddy's binding: the thread is
                      created or reopened, the message lands on the
                      timeline, the window moves, and the routing rule
                      (route.py) decides who answers. On any other binding:
                      nothing at all — workflows still hear the letter
                      through their own consumer.
    message.queued    we sent a TEMPLATE. Shown on an existing thread only
                      when Buddy's binding is also the template binding (a
                      merchant with one binding). Free-form replies never
                      come through here: their writer (reply.py, Buddy's
                      turns) put them on the timeline already.
    message.status    a receipt for one of our sends: nothing is written
                      (ticks are joined from the manifest at read) — the
                      thread showing that send is woken so the Inbox
                      re-reads them. Receipts name no customer, so this runs
                      before the customer check.

Idempotent by construction: the timeline's partial uniques make a replayed
letter write no row, and nothing a reader sees moves when it does (the
thread's upsert still locks the row, which is the point of it).
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.core.logger import logger
from app.crm.connectivity.contracts import (
    TOPIC_INBOUND,
    TOPIC_QUEUED,
    TOPIC_STATUS,
    buddy_binding,
    conversation_channels,
    conversation_settings,
    normalize_address,
    receipt_target,
)
from app.crm.conversations.db import DbTxn, atomically
from app.crm.conversations.db.accessors import (
    handoff as handoff_accessor,
    message as message_accessor,
    thread as thread_accessor,
)
from app.crm.conversations.realtime import wake
from app.crm.conversations.route import plan_inbound
from app.crm.conversations.schemas import TimelineRow
from app.crm.conversations.status import (
    AUTHOR_CUSTOMER,
    AUTHOR_WORKFLOW,
    KIND_INBOUND,
    KIND_OUTBOUND,
    SOURCE_AGENT,
    SOURCE_HUMAN,
    WAKE_MESSAGE,
    WAKE_RECEIPT,
)
from app.crm.record.contracts import RawEvent, canonical_path, derive_for, field_value

#: The longest preview the Inbox list shows.
PREVIEW_MAX = 140

#: The ``kind`` a template's message.queued letter carries (connectivity's
#: letters.KIND_TEMPLATE; spelled here, as record spells it, because this
#: module reads the letter's declared keys, not connectivity's code).
QUEUED_TEMPLATE = "template"
#: Senders that record their own timeline row at once (read-your-writes): a
#: projected copy would race that insert for the same message.
WRITER_RECORDED = (SOURCE_HUMAN, SOURCE_AGENT)


def _reader(event: RawEvent):
    derive = derive_for(event.source, event.topic)

    def value(name: str) -> Optional[str]:
        found = field_value(event.payload, canonical_path(name), derive)
        return str(found) if found not in (None, "") else None

    return value


def _preview(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= PREVIEW_MAX else text[: PREVIEW_MAX - 1] + "…"


async def consume_conversation_event(
    event: RawEvent,
    customer_id: Optional[str],
    handles: Optional[Dict[str, str]] = None,
    variables: Optional[Dict[str, Any]] = None,
) -> None:
    """One letter -> at most one timeline row. Letters this module does not
    project return quietly; a raise (a database blip) leaves the letter
    pending for the event worker's next pass."""
    if event.source not in conversation_channels():
        return
    if event.topic == TOPIC_STATUS:
        await _wake_on_receipt(event)
        return
    if customer_id is None:
        return
    if event.topic == TOPIC_INBOUND:
        await _project_inbound(event, customer_id)
    elif event.topic == TOPIC_QUEUED:
        await _project_template(event, customer_id)


async def _wake_on_receipt(event: RawEvent) -> None:
    """Wake the thread showing the send a receipt is about; a send no
    thread shows (a workflow's template on another binding) wakes nothing."""
    target = await receipt_target(event)
    if target is None:
        return
    provider_message_id, message_id = target
    thread_id = await message_accessor.thread_for_send(
        event.merchant_id, provider_message_id, message_id
    )
    if thread_id is not None:
        await wake(event.merchant_id, thread_id, WAKE_RECEIPT)


async def _project_inbound(event: RawEvent, customer_id: str) -> None:
    value = _reader(event)
    buddy = await buddy_binding(event.merchant_id, event.source)
    if buddy is None or value("business_address") != buddy.address:
        return  # not Buddy's binding: nothing here (R1)
    settings = await conversation_settings(event.merchant_id, event.source, buddy.id)
    kind = value("message_type") or "unknown"
    text = value("message_text") or (value("reply") if kind != "text" else None)
    caption = value("media_caption")
    body = {"type": kind, "text": text, "caption": caption}
    sender = value("sender_address")
    row = await atomically(
        _project_inbound_in_txn,
        event.merchant_id,
        event.source,
        customer_id,
        normalize_address(event.source, sender) if sender else None,
        buddy.id,
        event.id,
        event.external_id,
        body,
        _preview(text or caption) or f"[{kind}]",
        event.occurred_at or event.received_at,
        settings.default_agent_id,
        settings.claim_sla_minutes,
    )
    if row is not None:
        logger.bind(merchant_id=event.merchant_id, thread_id=row.conversation_id).info(
            f"inbound {event.id} projected onto thread {row.conversation_id}"
        )


async def _project_inbound_in_txn(
    txn: DbTxn,
    merchant_id: str,
    channel: str,
    customer_id: str,
    address: Optional[str],
    binding_id: str,
    event_raw_id: str,
    provider_message_id: str,
    body: Dict[str, Any],
    preview: str,
    occurred_at: datetime,
    agent_id: Optional[str],
    claim_sla_minutes: int,
) -> Optional[TimelineRow]:
    """ATOMIC: the timeline row, the thread's window and who answers it move
    together — the upsert locks the thread, so two letters for one customer
    queue, and a replayed letter (no row inserted) changes nothing."""
    # The contact key is the customer id, lowercased: one thread can never
    # be spelled two ways (the table's format CHECK insists).
    thread = await thread_accessor.ensure_thread(
        txn, merchant_id, channel, customer_id.lower(), customer_id, address, binding_id
    )
    row = await message_accessor.insert_inbound(
        txn,
        merchant_id,
        thread.id,
        KIND_INBOUND,
        AUTHOR_CUSTOMER,
        event_raw_id,
        provider_message_id,
        body,
        occurred_at,
    )
    if row is None:
        return None
    handoff = await handoff_accessor.open_for_thread(merchant_id, thread.id, txn)
    plan = plan_inbound(
        thread, handoff, agent_id, claim_sla_minutes, datetime.now(timezone.utc)
    )
    await thread_accessor.record_inbound(
        txn,
        merchant_id,
        thread.id,
        occurred_at,
        preview,
        plan.reopen,
        plan.bot_template_id,
        plan.bot_session_id,
        row.created_at,
    )
    await wake(merchant_id, thread.id, WAKE_MESSAGE, txn)
    return row


async def _project_template(event: RawEvent, customer_id: str) -> None:
    """A template we sent — onto her existing thread, when it went out on
    Buddy's binding (it is also the template binding). Never opens a thread:
    a template is not her starting a conversation."""
    value = _reader(event)
    if value("payload.kind") != QUEUED_TEMPLATE:
        return
    if value("payload.source_kind") in WRITER_RECORDED:
        return  # reply.py / Buddy's turn put it on the timeline already
    buddy = await buddy_binding(event.merchant_id, event.source)
    if buddy is None or not buddy.is_primary:
        return
    thread = await thread_accessor.thread_for_contact(
        event.merchant_id, event.source, customer_id.lower()
    )
    if thread is None:
        return
    occurred_at = event.occurred_at or event.received_at
    template_id = value("payload.template_id")
    row = await message_accessor.insert_outbound(
        None,
        event.merchant_id,
        thread.id,
        KIND_OUTBOUND,
        AUTHOR_WORKFLOW,
        None,
        value("payload.message_id"),
        None,
        {"type": QUEUED_TEMPLATE, "template_id": template_id},
        occurred_at,
    )
    if row is None:
        return
    await thread_accessor.touch_outbound(
        event.merchant_id, thread.id, occurred_at, _preview(f"Template · {template_id}")
    )
    await wake(event.merchant_id, thread.id, WAKE_MESSAGE)

"""Buddy moved to another binding (R7, D26): the old binding's open threads
are resolved quietly — sessions let go, open handoffs close
``binding_changed``, nothing is sent. From then on that binding does nothing
in the inbox; if she writes to Buddy's new binding, her thread reopens there.

Driven by connectivity's ``binding.buddy_moved`` letter (filed after the move
commits): one pass over the affected threads, once per move. A letter that
never arrives is caught by the window sweep, which never sends on a thread
whose binding is no longer Buddy's (sweeps.py).
"""

from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.crm.connectivity.contracts import TOPIC_BUDDY_MOVED, buddy_binding
from app.crm.conversations.db import DbTxn, atomically
from app.crm.conversations.db.accessors import (
    handoff as handoff_accessor,
    thread as thread_accessor,
)
from app.crm.conversations.realtime import wake
from app.crm.conversations.status import OUTCOME_BINDING_CHANGED, WAKE_STATE
from app.crm.record.contracts import RawEvent, canonical_path, derive_for, field_value


async def consume_buddy_moved(
    event: RawEvent,
    customer_id: Optional[str],
    handles: Optional[Dict[str, str]] = None,
    variables: Optional[Dict[str, Any]] = None,
) -> None:
    """One ``binding.buddy_moved`` letter -> the old binding's threads closed."""
    if event.topic != TOPIC_BUDDY_MOVED:
        return
    derive = derive_for(event.source, event.topic)
    old = field_value(event.payload, canonical_path("from_binding_id"), derive)
    if not old:
        return
    # A letter is history: Buddy may be back on that binding by the time it
    # is read (moved away and back, a retried or replayed letter). Its
    # threads are Buddy's again, so there is nothing to resolve.
    buddy = await buddy_binding(event.merchant_id, event.source)
    if buddy is not None and buddy.id == str(old):
        logger.bind(merchant_id=event.merchant_id, binding_id=str(old)).info(
            "Buddy moved back before the letter was read: nothing to resolve"
        )
        return
    resolved = await resolve_binding(event.merchant_id, str(old))
    if resolved:
        logger.bind(merchant_id=event.merchant_id, binding_id=str(old)).info(
            f"Buddy moved: resolved {len(resolved)} thread(s) on the old binding"
        )


async def resolve_binding(merchant_id: str, binding_id: str) -> List[str]:
    """Resolve every open thread on a binding Buddy no longer answers on."""
    return await atomically(_resolve_binding_in_txn, merchant_id, binding_id)


async def _resolve_binding_in_txn(
    txn: DbTxn, merchant_id: str, binding_id: str
) -> List[str]:
    """ATOMIC: the threads and their open handoffs close together — a
    thread is never left resolved with a handoff still asking for a person.
    Idempotent: a second pass finds nothing open."""
    ids = await thread_accessor.resolve_binding(txn, merchant_id, binding_id)
    await handoff_accessor.close_for_threads(
        txn, merchant_id, ids, OUTCOME_BINDING_CHANGED
    )
    if ids:
        await wake(merchant_id, None, WAKE_STATE, txn)
    return ids

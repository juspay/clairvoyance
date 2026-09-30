"""What became of a message we sent — the spine consumer that moves the
manifest along accepted -> sent -> delivered -> read (or failed).

Before this, receipts were filed on the spine and read by nobody: every
row stopped at 'accepted' and delivered_at / read_at were never written.
The inbox needs the ticks, and "why didn't she get it?" needs the failure.

**Generic, like the template consumer beside it.** A receipt is read
through the event catalog's DECLARED fields — ``status``,
``status_message_id``, ``status_error``, ``billed_category`` — resolved by
record's one decode engine, never by walking a provider's payload here.
Meta's field names are spent in record/extractors/whatsapp/status.py; a
second channel that declares the same four names is served unchanged.

**Registration** is one line in app/crm/worker_main.py (the record/consumers
inversion). A letter this consumer does not act on returns quietly: a
receipt for a message that is not ours — sent from the provider's own app
on the same number — is an ordinary outcome, not an error. The row write is
monotonic (apply_receipt_query), so duplicates and out-of-order receipts
are harmless.

**A receipt can beat its own row's stamp.** The provider's id reaches the
manifest when the dispatcher records the send's outcome, and a fast webhook
can be filed and consumed before that write lands. Raising would not help:
the event worker offers a pending letter again on its very next pass, with
no delay, and quarantines it after a handful of tries. So a young receipt
that matches no row is PARKED (crm_message_receipt_pending, migration 082)
and the letter completes normally:

  * the side that stamps the id — the dispatcher, a session send — applies
    whatever is parked for it straight after (``apply_parked``). The drain
    is one atom: a parked row leaves only with its receipt applied, in
    ladder order, so a failure part-way loses nothing;
  * this consumer, having parked, tries the row once more: if the stamp
    landed in between, it applies now; if not, the stamp has not committed
    yet, and its drain will find what was parked;
  * the dispatcher's sweep (``sweep_parked``) drains every message with
    receipts still parked — the retry for a drain that failed — then drops
    anything parked past PARK_GRACE: a receipt that old names a message this
    system did not send (the provider's own app on the same number).
    Quietly: it was never an error. The sweep rides the dispatcher's claim,
    so it runs wherever the dispatcher role runs (worker_main) and nowhere
    else.

A receipt already older than PARK_GRACE when it is read (a replay) is not
parked at all.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import logger
from app.crm.connectivity.db import DbTxn, atomically
from app.crm.connectivity.db.accessors import (
    message as message_accessor,
    receipt as receipt_accessor,
)
from app.crm.connectivity.schemas.message import ProviderReceipt
from app.crm.connectivity.status import (
    MESSAGE_DELIVERED,
    MESSAGE_FAILED,
    MESSAGE_READ,
    MESSAGE_SENT,
)
from app.crm.connectivity.topics import TOPIC_STATUS
from app.crm.record.contracts import RawEvent, canonical_path, derive_for, field_value

#: The receipt words this consumer acts on — the manifest's own words for
#: the same facts (status.py). Anything else a provider says is not a
#: delivery fact and is left alone.
RECEIPT_STATES = frozenset(
    {MESSAGE_SENT, MESSAGE_DELIVERED, MESSAGE_READ, MESSAGE_FAILED}
)

#: The order a message's parked receipts are applied in — the ladder, with
#: the terminal 'failed' last. The row write is monotonic but 'failed' is
#: terminal, so order decides a read-then-failed pair: laddered, the read
#: lands first and the late failure is refused (a message the customer has
#: cannot fail), exactly as when the two arrive live in that order.
LADDER = {MESSAGE_SENT: 0, MESSAGE_DELIVERED: 1, MESSAGE_READ: 2, MESSAGE_FAILED: 3}

#: How many messages with parked receipts one sweep drains.
SWEEP_BATCH = 100

#: How long a receipt waits, parked, for its message's row to learn the
#: provider's id. The stamp normally lands within a second of the send; the
#: grace covers a slow provider call (the send timeout) many times over.
#: Past it, the receipt names a message this system did not send.
PARK_GRACE = timedelta(minutes=10)


#: The catalog names a receipt is read by. Spelled here AND in record's
#: WhatsApp spec (rule 12 forbids the import); a test pins the two equal.
FIELD_STATE = "status"
FIELD_MESSAGE_ID = "status_message_id"
FIELD_ERROR = "status_error"
FIELD_CATEGORY = "billed_category"


def read_receipt(event: RawEvent) -> Optional[ProviderReceipt]:
    """PURE: the receipt one letter carries, or None when it is not one we
    act on (no id, no state, or a state off the delivery ladder)."""
    derive = derive_for(event.source, event.topic)

    def value(name: str) -> Optional[str]:
        found = field_value(event.payload, canonical_path(name), derive)
        return str(found) if found not in (None, "") else None

    provider_message_id = value(FIELD_MESSAGE_ID)
    state = value(FIELD_STATE)
    if provider_message_id is None or state is None or state not in RECEIPT_STATES:
        return None
    return ProviderReceipt(
        provider_message_id=provider_message_id,
        state=state,
        occurred_at=event.occurred_at,
        error_code=value(FIELD_ERROR) if state == MESSAGE_FAILED else None,
        pricing_category=value(FIELD_CATEGORY),
    )


async def consume_status_event(
    event: RawEvent,
    customer_id: Optional[str],
    handles: Optional[Dict[str, str]] = None,
    variables: Optional[Dict[str, Any]] = None,
) -> None:
    """One filed receipt -> at most one manifest row moved.

    ``customer_id`` is None for these letters by design (a receipt is about
    the MESSAGE, canon T13 col 14); the manifest row already names who.
    ``handles`` and ``variables`` are accepted for the registry's shape and
    ignored. A raise (a database blip) leaves the letter pending for the
    next pass.
    """
    if event.topic != TOPIC_STATUS:
        return
    receipt = read_receipt(event)
    if receipt is None:
        return
    status = await _apply(event.merchant_id, receipt)
    if status is None:
        if datetime.now(timezone.utc) - event.received_at >= PARK_GRACE:
            logger.debug(
                f"receipt {event.id}: no message of ours carries this id — ignored"
            )
            return
        # Too early, not foreign (yet): park it for the stamp's own drain,
        # then look once more — see the module docstring.
        await receipt_accessor.park_receipt(event.merchant_id, receipt)
        status = await _apply(event.merchant_id, receipt)
        if status is None:
            logger.debug(
                f"receipt {event.id}: parked until its message's id is recorded"
            )
            return
        await receipt_accessor.unpark(
            event.merchant_id, receipt.provider_message_id, receipt.state
        )
    logger.bind(
        merchant_id=event.merchant_id,
        receipt=receipt.state,
        status=status,
        reason=receipt.error_code,
    ).info(f"receipt {receipt.state} applied — message now {status}")


async def _apply(merchant_id: str, receipt: ProviderReceipt) -> Optional[str]:
    return await message_accessor.apply_receipt(
        merchant_id,
        receipt.provider_message_id,
        receipt.state,
        receipt.occurred_at,
        receipt.error_code,
        receipt.pricing_category,
    )


async def apply_parked(merchant_id: str, provider_message_id: Optional[str]) -> None:
    """Apply every receipt parked for a message whose row just learned its
    provider id — called by the side that stamped it, and by the sweep.

    Never raises: the stamp already landed, and a receipt that cannot be
    applied now is the sweep's to retry, not the send's to fail over. Nothing
    is lost on a raise — the atom rolls back and the rows stay parked.
    """
    if not provider_message_id:
        return
    try:
        applied = await atomically(
            _apply_parked_in_txn, merchant_id, provider_message_id
        )
    except Exception as e:  # noqa: BLE001 — see the docstring
        logger.opt(exception=e).warning(
            f"parked receipts for {provider_message_id} not applied — "
            f"left parked for the sweep"
        )
        return
    for receipt, status in applied:
        logger.bind(
            merchant_id=merchant_id,
            receipt=receipt.state,
            status=status,
            reason=receipt.error_code,
        ).info(f"parked receipt {receipt.state} applied — message now {status}")


async def _apply_parked_in_txn(
    txn: DbTxn, merchant_id: str, provider_message_id: str
) -> List[Tuple[ProviderReceipt, str]]:
    """ATOMIC: a parked receipt's removal shares fate with its row write —
    a row is unparked only once its receipt has moved the manifest, so a
    raise part-way rolls every removal back and the sweep tries again.

    Applied in LADDER order, whatever order they were parked in. A receipt
    that still matches no row stays parked (its stamp is not visible yet, or
    it is foreign — the grace decides)."""
    parked = await receipt_accessor.lock_parked(txn, merchant_id, provider_message_id)
    applied: List[Tuple[ProviderReceipt, str]] = []
    for receipt in sorted(parked, key=lambda r: LADDER.get(r.state, len(LADDER))):
        status = await message_accessor.apply_receipt(
            merchant_id,
            receipt.provider_message_id,
            receipt.state,
            receipt.occurred_at,
            receipt.error_code,
            receipt.pricing_category,
            txn=txn,
        )
        if status is None:
            continue
        await receipt_accessor.unpark(
            merchant_id, receipt.provider_message_id, receipt.state, txn=txn
        )
        applied.append((receipt, status))
    return applied


async def sweep_parked() -> int:
    """The dispatcher's sweep: drain the messages whose row now carries the
    id but whose receipts are still parked (the retry for a drain that
    failed, or a stamp whose own drain lost the race), then drop what has
    waited past PARK_GRACE. Returns how many were dropped."""
    for merchant_id, provider_message_id in await receipt_accessor.parked_messages(
        SWEEP_BATCH
    ):
        await apply_parked(merchant_id, provider_message_id)
    return await receipt_accessor.expire_parked(int(PARK_GRACE.total_seconds()))

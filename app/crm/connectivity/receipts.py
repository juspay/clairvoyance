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
on the same binding — is an ordinary outcome, not an error. The row write is
monotonic (apply_receipt_query), so duplicates and out-of-order receipts
are harmless.

**A receipt can beat its own row's stamp.** The provider's id reaches the
manifest when the dispatcher records the send's outcome, and a fast webhook
can be filed and consumed before that write lands: it matches no row, and
this consumer lets it go. It is not lost — the letter stays on the spine,
and the side that stamps the id (the dispatcher, a session send) reads the
message's status letters back straight after the stamp and applies them
(``apply_filed_receipts``). The two sides cannot both miss: the stamp
commits before the read-back, so a letter filed before it is found there,
and one filed after it is consumed after the stamp and matches. A receipt
that matches no row at all names a message this system did not send (the
provider's own app on the same binding).
"""

from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import logger
from app.crm.connectivity.db.accessors import message as message_accessor
from app.crm.connectivity.ingress import status_letter_keys
from app.crm.connectivity.reasons import REASON_PROVIDER_REJECTED
from app.crm.connectivity.schemas.message import MessageTick, ProviderReceipt
from app.crm.connectivity.status import (
    MESSAGE_DELIVERED,
    MESSAGE_FAILED,
    MESSAGE_READ,
    MESSAGE_SENT,
)
from app.crm.connectivity.topics import TOPIC_STATUS
from app.crm.record.contracts import (
    RawEvent,
    canonical_path,
    derive_for,
    field_value,
    letters_by_external_ids,
)

#: The receipt words this consumer acts on — the manifest's own words for
#: the same facts (status.py). Anything else a provider says is not a
#: delivery fact and is left alone.
RECEIPT_STATES = frozenset(
    {MESSAGE_SENT, MESSAGE_DELIVERED, MESSAGE_READ, MESSAGE_FAILED}
)

#: The order a message's read-back receipts are applied in — the ladder,
#: with the terminal 'failed' last. The row write is monotonic but 'failed'
#: is terminal, so order decides a read-then-failed pair: laddered, the read
#: lands first and the late failure is refused (a message the customer has
#: cannot fail), exactly as when the two arrive live in that order.
LADDER = {MESSAGE_SENT: 0, MESSAGE_DELIVERED: 1, MESSAGE_READ: 2, MESSAGE_FAILED: 3}


#: The catalog names a receipt is read by. Spelled here AND in each record
#: channel spec that declares receipts (WhatsApp's today; rule 12 forbids the
#: import); a test pins the two equal.
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
        # A failure always names a reason, even when the provider sent no
        # code — a row with no reason is a support ticket nobody can answer.
        error_code=(
            (value(FIELD_ERROR) or REASON_PROVIDER_REJECTED)
            if state == MESSAGE_FAILED
            else None
        ),
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
        # Ahead of its row's stamp (the dispatcher reads it back), or not a
        # message of ours — see the module docstring.
        logger.debug(f"receipt {event.id}: no message carries this id yet")
        return
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


async def apply_filed_receipts(
    merchant_id: str, channel: str, provider_message_id: Optional[str]
) -> None:
    """Apply the receipts already filed for a message whose row has just
    learned its provider id — the dispatcher calls this straight after the
    stamp. Read back from the spine by the ids the channel's face files them
    under, applied in LADDER order; the write is monotonic, so one the
    consumer already applied changes nothing.

    Never raises: the send happened and its outcome is recorded. A receipt
    that cannot be read back now is logged, and the message's later
    receipts still land on their own."""
    if not provider_message_id:
        return
    keys = status_letter_keys(channel, provider_message_id, LADDER)
    if keys is None:
        return
    source, external_ids = keys
    try:
        letters = await letters_by_external_ids(merchant_id, source, external_ids)
        filed = [r for r in (read_receipt(letter) for letter in letters) if r]
        for receipt in sorted(filed, key=lambda r: LADDER.get(r.state, len(LADDER))):
            status = await _apply(merchant_id, receipt)
            if status is not None:
                logger.bind(
                    merchant_id=merchant_id,
                    receipt=receipt.state,
                    status=status,
                    reason=receipt.error_code,
                ).info(f"filed receipt {receipt.state} applied — message now {status}")
    except Exception as e:  # noqa: BLE001 — see the docstring
        logger.opt(exception=e).warning(
            f"receipts filed for {provider_message_id} not read back"
        )


async def receipt_target(event: RawEvent) -> Optional[Tuple[str, Optional[str]]]:
    """Which of our sends a receipt letter is about: (the provider's id, our
    row's id — None when no row of ours carries it yet). None for a letter
    that is not a receipt we act on. A read: the receipt itself is applied
    by consume_status_event."""
    if event.topic != TOPIC_STATUS:
        return None
    receipt = read_receipt(event)
    if receipt is None:
        return None
    message_id = await message_accessor.message_id_by_provider(
        event.merchant_id, receipt.provider_message_id
    )
    return receipt.provider_message_id, message_id


async def message_ticks(
    merchant_id: str, message_ids: List[str]
) -> Dict[str, MessageTick]:
    """What receipts have made of these rows, by id — the conversations
    timeline shows its ticks from here. Rows of another merchant read as
    absent."""
    ticks = await message_accessor.message_ticks(merchant_id, message_ids)
    return {tick.id: tick for tick in ticks}

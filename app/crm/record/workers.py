"""The event worker's pass (CRM_ROLE=event-worker, app/crm/worker_main.py):
claim a batch, then per row extract -> resolve() -> assert_facts() -> entry
rules -> stamp, one commit."""

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.core.logger import logger
from app.crm.identity.contracts import assert_facts, resolve as crm_resolve
from app.crm.outreach.contracts import consume_attributed_event
from app.crm.record.db import DbTxn, accessor, atomically, savepoint
from app.crm.record.schemas import Extracted, RawEvent
from app.crm.shared.normalize import normalize_email, normalize_phone

# Producer's payload key -> canon attribute name (T05). A producer with a
# different shape brings its own map.
FACT_KEYS = {
    "customer_name": "name",
    "locale": "locale",
    "timezone": "timezone",
}


def _extract_flat(payload: Dict[str, Any]) -> Extracted:
    """The flat shape: handles and facts as top-level keys. Every buddy
    mirror (lead-api, telephony) sends it, and it is what a new producer
    gets by default — the name describes the payload, not the producer."""
    phone = payload.get("customer_mobile_number")
    return Extracted(
        handles={"phone": phone} if phone else {},
        facts={
            attribute: payload[key]
            for key, attribute in FACT_KEYS.items()
            if payload.get(key)
        },
    )


def _extract_shopify(payload: Dict[str, Any]) -> Extracted:
    """Shopify's own order/checkout body, as the relay forwards it.

    The relay is a pipe: it carries Shopify's words unopened, so the
    nesting arrives intact and this is where it gets read. Shopify puts a
    phone in up to three places and the top-level one is usually null; a
    guest checkout carries no customer object at all, which is why the
    shipping address is the fallback for both handle and name.

    The name is never defaulted. A placeholder like "Customer" would
    reach assert_facts as a genuine name claim and overwrite what we
    actually know about this person — absent is absent.
    """
    customer = payload.get("customer") or {}
    address = payload.get("shipping_address") or payload.get("billing_address") or {}

    # Most specific first: the customer record beats the shipping contact.
    raw_phone = customer.get("phone") or payload.get("phone") or address.get("phone")
    raw_email = customer.get("email") or payload.get("email")

    handles: Dict[str, str] = {}
    if raw_phone:
        phone = normalize_phone(str(raw_phone))
        if phone:
            handles["phone"] = phone
    if raw_email:
        email = normalize_email(str(raw_email))
        if email:
            handles["email"] = email

    first = customer.get("first_name") or address.get("first_name") or ""
    last = customer.get("last_name") or address.get("last_name") or ""
    name = " ".join(part for part in (str(first), str(last)) if part).strip()

    return Extracted(handles=handles, facts={"name": name} if name else {})


# source -> extractor. A new channel is one registration here; an
# unregistered source falls back to the flat shape.
Extractor = Callable[[Dict[str, Any]], Extracted]
EXTRACTORS: Dict[str, Extractor] = {
    "lead-api": _extract_flat,
    "telephony": _extract_flat,
    "shopify": _extract_shopify,
}
DEFAULT_EXTRACTOR: Extractor = _extract_flat


async def run_pass(limit: int) -> List[RawEvent]:
    """One whole pass, claim through commit. Returns the rows it CLAIMED, so a
    fully quarantined batch still reads as work rather than as an empty queue."""
    return await atomically(_pass_in_txn, limit)


async def _pass_in_txn(txn: DbTxn, limit: int) -> List[RawEvent]:
    """ATOMIC: the claim and every stamp it authorises share one commit — the
    FOR UPDATE SKIP LOCKED lock lives exactly as long as this transaction, so
    a stamp can never outlive its claim. resolve(), assert_facts() and the
    entry-rules call are NOT in this atom: each commits independently, so a
    replay is made safe by their idempotency, not by this rollback."""
    events = await accessor.claim_pending_events(txn, limit)
    _log_queue_lag(events, limit)
    for event in events:
        try:
            async with savepoint(txn):
                await _process_one(txn, event)
        except Exception as e:
            logger.error(f"event {event.id} pass failed, will retry next poll: {e}")
    return events


async def _process_one(txn: DbTxn, event: RawEvent) -> None:
    """One row, inside the caller's savepoint. A quarantine stamped itself
    already and names no customer, so it returns early."""
    customer_id = await _run_processor(txn, event)
    if customer_id is None:
        return
    await _consume_attributed_event(event, customer_id)
    await accessor.stamp_event(txn, str(event.id), customer_id)


async def _consume_attributed_event(event: RawEvent, customer_id: str) -> None:
    """Entry-rules slot: per row, inside its savepoint, before its stamp, so a
    poison rule costs one row per poll. A raise here leaves the row pending."""
    await consume_attributed_event(event, customer_id)


async def _run_processor(txn: DbTxn, event: RawEvent) -> Optional[str]:
    """extract -> resolve() (or pass through a set customer_id) -> assert_facts().
    Quarantines what it cannot attribute; a failed assert_facts never fails the row."""
    extract = EXTRACTORS.get(event.source, DEFAULT_EXTRACTOR)
    try:
        extracted = extract(event.payload)
    except Exception as e:
        await accessor.quarantine_event(txn, str(event.id), f"extractor_error: {e}")
        return None

    customer_id = event.customer_id
    if not customer_id:
        if not extracted.handles:
            await accessor.quarantine_event(txn, str(event.id), "no_handle")
            return None
        try:
            customer_id = str(
                await crm_resolve(
                    event.merchant_id,
                    extracted.handles,
                    evidence="observed",
                    source=event.source,
                )
            )
        except ValueError as e:
            await accessor.quarantine_event(txn, str(event.id), f"unresolvable: {e}")
            return None

    if extracted.facts:
        try:
            await assert_facts(
                event.merchant_id,
                customer_id,
                extracted.facts,
                evidence="observed",
                source=event.source,
            )
        except Exception as e:
            logger.warning(f"event {event.id}: assert_facts failed, dropping: {e}")
    return customer_id


def _log_queue_lag(events: List[RawEvent], limit: int) -> None:
    """How long the oldest claimed row sat unprocessed — the alert that rises
    whatever the cause (received_at is timestamptz NOT NULL, so always aware)."""
    if not events:
        return
    oldest = min(e.received_at for e in events)
    lag_s = (datetime.now(timezone.utc) - oldest).total_seconds()
    logger.info(
        f"event-worker pass: claimed={len(events)} lag_s={lag_s:.1f} "
        f"queue_deeper_than_batch={len(events) >= limit}"
    )


async def observe_processed_event(event: RawEvent) -> None:
    """The scaffold's per-row hook, after run_pass committed. Observer only —
    every write already happened, so never touch the database here."""
    logger.debug(f"event {event.id} ({event.source}/{event.topic}) processed")

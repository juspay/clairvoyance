"""The event worker's pass (CRM_ROLE=event-worker, app/crm/worker_main.py):
claim a batch, then per row extract -> resolve() -> assert_facts() -> entry
rules -> stamp, one commit.

The pass knows no source by name: which extractor reads a payload is the
registry's business (record/extractors), and this file only asks it."""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from app.core.config.static import CRM_EVENT_MAX_ATTEMPTS
from app.core.logger import logger
from app.crm.identity.contracts import assert_facts, resolve as crm_resolve
from app.crm.record.consumers import consumers
from app.crm.record.db import DbTxn, accessor, atomically, savepoint
from app.crm.record.extractors import DEFAULT_EXTRACTOR, EXTRACTORS
from app.crm.record.schemas import RawEvent


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
            await _after_failed_row(txn, event, e)
    return events


async def _after_failed_row(txn: DbTxn, event: RawEvent, error: Exception) -> None:
    """The row's savepoint has rolled back; ``txn`` is still valid. The
    claim already spent this attempt (062), so a row at the ceiling is
    quarantined here as its own statement, inside its own savepoint — a
    consumer that raises deterministically used to sit at the head of the
    queue forever, re-running resolve()/assert_facts() every poll. Below
    the ceiling the row stays pending and returns next poll, as before.
    Quarantine, never delete: replay is the recovery. The log names the
    letter, never its payload."""
    if event.attempts < CRM_EVENT_MAX_ATTEMPTS:
        logger.error(
            f"event {event.id} ({event.source}/{event.topic}) pass failed on "
            f"attempt {event.attempts}, will retry next poll: {error}"
        )
        return
    reason = f"consumer_error after {event.attempts} attempts: {error}"
    try:
        # Its own savepoint: a failed statement on the bare transaction
        # would abort it, and every later row's stamp would be lost at
        # commit. Inside a savepoint only this write rolls back.
        async with savepoint(txn):
            await accessor.quarantine_event(txn, str(event.id), reason)
    except Exception as quarantine_error:
        # The batch must not die for one row's bookkeeping; the row stays
        # pending and the next claim tries the quarantine again.
        logger.error(
            f"event {event.id} ({event.source}/{event.topic}) could not be "
            f"quarantined, stays pending: {quarantine_error}"
        )
        return
    logger.error(
        f"event {event.id} ({event.source}/{event.topic}) quarantined after "
        f"{event.attempts} attempts: {error}"
    )


async def _process_one(txn: DbTxn, event: RawEvent) -> None:
    """One row, inside the caller's savepoint. A quarantine stamped itself
    already and names no customer, so it returns early."""
    customer_id, handles = await _run_processor(txn, event)
    if customer_id is None:
        return
    await _consume_attributed_event(event, customer_id, handles)
    await accessor.stamp_event(txn, str(event.id), customer_id)


async def _consume_attributed_event(
    event: RawEvent, customer_id: str, handles: Dict[str, str]
) -> None:
    """Consumer slot: per row, inside its savepoint, before its stamp, so a
    poison consumer costs one row per poll. A raise here leaves the row
    pending. WHO runs is the registry's business (record/consumers.py,
    filled by worker_main) — this file imports no subscriber, so a
    subscriber may read record's contracts without ever forming a cycle.

    The extractor's handles ride along so no consumer re-hunts what this
    pass already found. Two searches would drift — and did: a Shopify
    order with its phone only in customer.default_address resolved here and
    then parked at the first call node, because the payload re-search did
    not know that path."""
    for consume in consumers():
        await consume(event, customer_id, handles)


async def _run_processor(
    txn: DbTxn, event: RawEvent
) -> Tuple[Optional[str], Dict[str, str]]:
    """extract -> resolve() (or pass through a set customer_id) -> assert_facts().
    Quarantines what it cannot attribute; a failed assert_facts never fails the row.

    Returns the customer AND the handles the extractor found, so the one
    source-aware discovery in the system is this one."""
    extract = EXTRACTORS.get(event.source, DEFAULT_EXTRACTOR)
    try:
        extracted = extract(event.payload)
    except Exception as e:
        await accessor.quarantine_event(txn, str(event.id), f"extractor_error: {e}")
        return None, {}

    customer_id = event.customer_id
    if not customer_id:
        if not extracted.handles:
            await accessor.quarantine_event(txn, str(event.id), "no_handle")
            return None, {}
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
            return None, {}

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
    return customer_id, extracted.handles


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

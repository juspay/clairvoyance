"""The event worker's pass (CRM_ROLE=event-worker, app/crm/worker_main.py):
claim a batch, then per row extract -> resolve() -> assert_facts() -> entry
rules -> stamp, one commit.

The pass knows no source by name: which spec reads a payload is the
catalog's business (record/catalog, record/extractors), and this file only
asks it."""

from datetime import datetime, timezone
from typing import Any, Dict, List, NamedTuple, Optional

from app.core.config.static import CRM_EVENT_MAX_ATTEMPTS
from app.core.logger import logger
from app.crm.identity.contracts import assert_facts, resolve as crm_resolve
from app.crm.record import catalog
from app.crm.record.consumers import consumers
from app.crm.record.db import DbTxn, accessor, atomically, savepoint
from app.crm.record.extractors import EXTRACTORS, engine
from app.crm.record.extractors.engine import EMPTY_SPEC, DecodeSpec
from app.crm.record.schemas import ABOUT_MERCHANT, Extracted, RawEvent


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


class _Processed(NamedTuple):
    """What the processor made of one row: the customer it is about (None
    for a merchant-level letter — canon T13 col 14's "processed but not
    about a person"), the handles the spec found the person by, the
    template fill-ins the catalog declared for this (source, topic) —
    resolved by the engine at decode so no consumer re-reads the payload —
    and whether the row quarantined itself (then nothing else may touch
    it)."""

    customer_id: Optional[str]
    handles: Dict[str, str]
    variables: Dict[str, Any]
    quarantined: bool


async def _process_one(txn: DbTxn, event: RawEvent) -> None:
    """One row, inside the caller's savepoint. A quarantine stamped itself
    already and names no customer, so it returns early. A merchant-level
    letter (``Extracted.about == "merchant"``) is processed with a NULL
    customer: every consumer still hears it, and the stamp writes
    processed_at with customer_id NULL — forever, correctly."""
    await _discover_topic(txn, event)
    outcome = await _run_processor(txn, event)
    if outcome.quarantined:
        return
    await _consume_attributed_event(
        event, outcome.customer_id, outcome.handles, outcome.variables
    )
    await accessor.stamp_event(txn, str(event.id), outcome.customer_id)


async def _consume_attributed_event(
    event: RawEvent,
    customer_id: Optional[str],
    handles: Dict[str, str],
    variables: Optional[Dict[str, Any]] = None,
) -> None:
    """Consumer slot: per row, inside its savepoint, before its stamp, so a
    poison consumer costs one row per poll. A raise here leaves the row
    pending. WHO runs is the registry's business (record/consumers.py,
    filled by worker_main) — this file imports no subscriber, so a
    subscriber may read record's contracts without ever forming a cycle.

    ``customer_id`` is None for a merchant-level letter (a template review,
    an account notice): the letter still reaches every consumer, and each
    decides whether a letter with no person is its business.

    The spec's handles and variables ride along so no consumer re-hunts
    what this pass already found. Two searches would drift — and did: a
    Shopify order with its phone only in customer.default_address resolved
    here and then parked at the first call node, because the payload
    re-search did not know that path."""
    for consume in consumers():
        await consume(event, customer_id, handles, variables or {})


async def _discover_topic(txn: DbTxn, event: RawEvent) -> None:
    """The unregistered-topic nudge (canon T24): one INSERT per new
    (merchant, source, topic) EVER; the in-process known-set means the hot
    path never probes. Inside the row's savepoint — a failure here fails
    the row, so the nudge can never be lost silently."""
    key = (event.merchant_id, event.source, event.topic)
    if catalog.is_known(key):
        return
    if catalog.code_spec(event.source, event.topic) is not None:
        # The code layer already declares this event: there is nothing to
        # nudge anyone to register, and a detected row here would be noise
        # the merge has to ignore.
        catalog.mark_known(key)
        return
    await accessor.insert_detected_schema(
        txn, event.merchant_id, event.source, event.topic
    )
    catalog.mark_known(key)


def _extract(event: RawEvent, spec: DecodeSpec) -> Extracted:
    """The decode step (pure). One engine, two spec sources: the letter is
    read by its spec — a code CatalogEntry or the vendor's registration —
    over the flat shape. The few sources still decoding by hand (EXTRACTORS)
    keep their function until they become specs; a code-catalog source is
    never in both (pinned)."""
    extract = EXTRACTORS.get(event.source)
    if extract is not None:
        return extract(event.payload)
    return engine.extract(event.payload, spec)


async def _run_processor(txn: DbTxn, event: RawEvent) -> _Processed:
    """extract -> resolve() (or pass through a set customer_id) -> assert_facts().
    Quarantines what it cannot attribute; a failed assert_facts never fails the row.

    Returns the customer, the handles the engine found the person by, and
    the template variables the catalog declared — so the one source-aware
    read of the payload in the system is this one. A letter the spec
    declares ABOUT THE MERCHANT skips resolve() and facts — there is no
    person to find or describe — and comes back with no customer and no
    quarantine: processed, NULL, correctly (canon T13 col 14)."""
    spec = EMPTY_SPEC
    if event.source not in EXTRACTORS:
        # Code layer: pure. Registered layer: a cached read (T24 stays cold).
        # A failure here raises: the row stays pending and returns next poll
        # — never a terminal quarantine.
        spec = await catalog.decode_spec(event.merchant_id, event.source, event.topic)
    try:
        extracted = _extract(event, spec)
    except Exception as e:
        await accessor.quarantine_event(txn, str(event.id), f"extractor_error: {e}")
        return _Processed(None, {}, {}, quarantined=True)
    customer_id = event.customer_id
    if not customer_id and extracted.about == ABOUT_MERCHANT:
        return _Processed(None, {}, extracted.variables, quarantined=False)
    if not customer_id:
        if not extracted.handles:
            await accessor.quarantine_event(txn, str(event.id), "no_handle")
            return _Processed(None, {}, {}, quarantined=True)
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
            return _Processed(None, {}, {}, quarantined=True)
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
    return _Processed(
        customer_id, extracted.handles, extracted.variables, quarantined=False
    )


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

"""The event worker end to end: the pass in app/crm/record/workers.py and the
shared drain loop in app/crm/shared/worker.py that drives it.

The pass: extractor registry -> resolve() -> assert_facts() -> entry rules ->
one closing UPDATE, with a per-row savepoint that keeps one bad row from
poisoning the batch. A quarantine is terminal (no second UPDATE); a
fact-assertion failure must not fail the row (nothing downstream depends on
it); an entry-rules failure MUST fail the row (an enrolment silently dropped
is worse than a retry). Verified against workers.py's private
_pass_in_txn/_process_one directly with a fake txn, so none of this needs a
real database.

The loop: full batches loop immediately, empty batches back off with jitter up
to a 5s cap, a row failure never stops the batch, a claim failure backs off
instead of crashing the worker, stop_event ends the loop within one interval,
and a heartbeat proves an idle worker is still alive. Timing is asserted
through a recording fake for asyncio.sleep-equivalents (the delays
run_drain_loop ASKED for), not through wall-clock elapsed time: the loop's
contract is the delays it chooses, and a CI box that stalls mid-test must not
turn that contract red.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, cast

import pytest

import app.crm.record.consumers as record_consumers
import app.crm.record.extractors.shopify as shopify_extractor
import app.crm.record.workers as workers
import app.crm.shared.worker as worker_mod
from app.crm.record.db import DbTxn
from app.crm.record.db.decoder import decode_raw_event
from app.crm.record.db.queries import claim_pending_events_query
from app.crm.record.extractors import EXTRACTORS
from app.crm.record.schemas import Extracted, RawEvent
from app.crm.shared.worker import run_drain_loop

# ---------------------------------------------------------------------------
# The pass (app/crm/record/workers.py)
# ---------------------------------------------------------------------------


async def _no_plans_live(
    event: RawEvent, customer_id: str, handles: Dict[str, str]
) -> None:
    """The entry-rules consumer with no plans live: the pass must run the
    same with or without outreach reacting."""
    return None


@pytest.fixture(autouse=True)
def _no_outreach(monkeypatch: pytest.MonkeyPatch) -> None:
    # The registry, not the import: workers.py no longer knows any
    # subscriber by name (rule 12) — tests wire the slot the same way
    # worker_main does.
    monkeypatch.setattr(record_consumers, "_CONSUMERS", [_no_plans_live])


class _FakeSavepoint:
    async def __aenter__(self) -> "_FakeSavepoint":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeTxnImpl:
    def transaction(self) -> _FakeSavepoint:
        return _FakeSavepoint()


def _fake_txn() -> DbTxn:
    return cast(DbTxn, _FakeTxnImpl())


class _FakeAccessor:
    def __init__(self) -> None:
        self.stamped: List[Tuple[str, Optional[str]]] = []
        self.quarantined: List[Tuple[str, str]] = []

    async def stamp_event(
        self, conn: Any, event_id: str, customer_id: Optional[str]
    ) -> None:
        self.stamped.append((event_id, customer_id))

    async def quarantine_event(self, conn: Any, event_id: str, reason: str) -> None:
        self.quarantined.append((event_id, reason))


def _event(
    event_id: str = "evt-1",
    source: str = "lead-api",
    topic: str = "lead.pushed",
    customer_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    attempts: int = 1,
) -> RawEvent:
    return RawEvent(
        id=event_id,
        merchant_id="m1",
        source=source,
        topic=topic,
        schema_version="1",
        external_id=f"ext-{event_id}",
        payload=(
            payload
            if payload is not None
            else {"customer_mobile_number": "+919999999999"}
        ),
        received_at=datetime.now(timezone.utc),
        customer_id=customer_id,
        attempts=attempts,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_missing_phone_quarantines_and_never_stamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)

    event = _event(payload={"lead_id": "lead-1"})
    _run(workers._process_one(_fake_txn(), event))

    assert fake_accessor.quarantined == [("evt-1", "no_handle")]
    # quarantine_event already stamped processed_at itself -- a second
    # stamp_event call would be a redundant UPDATE.
    assert fake_accessor.stamped == []


def test_a_merchant_level_letter_is_processed_with_no_customer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Canon T13 col 14: "processed but not about a person (template.status,
    # receipts) — NULL forever, correctly". A template review names no
    # person BY DESIGN: not a quarantine, not a resolve — a NULL stamp, and
    # every consumer still hears it (the template-status consumer is
    # exactly who it is for).
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)

    async def never_resolve(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("a merchant-level letter must never resolve()")

    async def never_assert(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a merchant-level letter has no person to describe")

    monkeypatch.setattr(workers, "crm_resolve", never_resolve)
    monkeypatch.setattr(workers, "assert_facts", never_assert)
    monkeypatch.setitem(
        EXTRACTORS, "registry-test", lambda payload: Extracted(about="merchant")
    )
    heard: List[Tuple[Optional[str], Any]] = []

    async def consumer(
        event: RawEvent, customer_id: Optional[str], handles: Any
    ) -> None:
        heard.append((customer_id, handles))

    monkeypatch.setattr(record_consumers, "_CONSUMERS", [consumer])

    event = _event(
        source="registry-test",
        topic="template.status",
        payload={"event": "APPROVED", "message_template_id": "t-1"},
    )
    _run(workers._process_one(_fake_txn(), event))

    assert fake_accessor.quarantined == []
    assert fake_accessor.stamped == [("evt-1", None)]
    assert heard == [(None, {})]


def test_a_customer_letter_with_no_handle_still_quarantines_by_default() -> None:
    # The word defaults to customer, so nothing an extractor does not say
    # changes: no handle stays a quarantine, replayable when the extractor
    # learns the shape.
    assert Extracted().about == "customer"
    assert Extracted(handles={}).about == "customer"


def test_a_pretrusted_customer_id_wins_over_a_merchant_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A producer that already stamped the customer (the voice mirrors) is
    # believed; the extractor's word only matters when nobody is named.
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)
    monkeypatch.setitem(
        EXTRACTORS, "registry-test", lambda payload: Extracted(about="merchant")
    )
    event = _event(source="registry-test", customer_id="cust-77", payload={})
    _run(workers._process_one(_fake_txn(), event))
    assert fake_accessor.stamped == [("evt-1", "cust-77")]


def test_unresolvable_phone_quarantines_and_never_stamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)

    async def fake_resolve(*args: Any, **kwargs: Any) -> str:
        raise ValueError("no usable handle")

    monkeypatch.setattr(workers, "crm_resolve", fake_resolve)

    event = _event()
    _run(workers._process_one(_fake_txn(), event))

    assert len(fake_accessor.quarantined) == 1
    eid, reason = fake_accessor.quarantined[0]
    assert eid == "evt-1"
    assert reason.startswith("unresolvable:")
    assert fake_accessor.stamped == []


def test_a_raising_extractor_quarantines_rather_than_retrying_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payload the registered extractor cannot read is deterministically
    bad: retrying it every poll would stall the queue behind it."""
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)

    def boom(payload: Dict[str, Any]) -> Any:
        raise KeyError("mandatory field missing")

    monkeypatch.setitem(EXTRACTORS, "lead-api", boom)

    _run(workers._process_one(_fake_txn(), _event()))

    assert len(fake_accessor.quarantined) == 1
    assert fake_accessor.quarantined[0][1].startswith("extractor_error:")
    assert fake_accessor.stamped == []


def test_resolved_phone_asserts_canon_named_facts_from_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extractor translates the producer's wire key (customer_name)
    into canon T05's attribute vocabulary (name) -- identity never learns
    a producer's spelling."""
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)

    async def fake_resolve(merchant_id: str, handles: Dict[str, str], **kw: Any) -> str:
        assert merchant_id == "m1"
        assert handles == {"phone": "+919999999999"}
        assert kw["evidence"] == "observed"  # a merchant push is testimony
        return "cust-99"

    facts_calls: List[Tuple[str, str, Dict[str, Any]]] = []

    async def fake_assert_facts(
        merchant_id: str, customer_id: str, facts: Dict[str, Any], **kw: Any
    ) -> None:
        facts_calls.append((merchant_id, customer_id, facts))

    monkeypatch.setattr(workers, "crm_resolve", fake_resolve)
    monkeypatch.setattr(workers, "assert_facts", fake_assert_facts)

    event = _event(
        payload={
            "lead_id": "lead-1",
            "customer_mobile_number": "+919999999999",
            "customer_name": "Ravi",
            "irrelevant": "x",
        }
    )
    _run(workers._process_one(_fake_txn(), event))

    assert facts_calls == [("m1", "cust-99", {"name": "Ravi"})]
    assert fake_accessor.stamped == [("evt-1", "cust-99")]
    assert fake_accessor.quarantined == []


def test_no_row_is_minted_as_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "declared" is the customer's OWN statement. A merchant push minted
    at that rung could overwrite handles the customer gave us themselves
    (ADR 0021's ladder), so no row this worker processes may sit there."""
    monkeypatch.setattr(workers, "accessor", _FakeAccessor())

    seen: List[str] = []

    async def fake_resolve(merchant_id: str, handles: Dict[str, str], **kw: Any) -> str:
        seen.append(kw["evidence"])
        return "cust-1"

    async def fake_assert_facts(
        merchant_id: str, customer_id: str, facts: Dict[str, Any], **kw: Any
    ) -> None:
        seen.append(kw["evidence"])

    monkeypatch.setattr(workers, "crm_resolve", fake_resolve)
    monkeypatch.setattr(workers, "assert_facts", fake_assert_facts)

    event = _event(
        payload={"customer_mobile_number": "+919999999999", "customer_name": "Ravi"}
    )
    _run(workers._process_one(_fake_txn(), event))

    assert seen == ["observed", "observed"]


def test_unrecognized_source_falls_back_to_the_buddy_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source with no registration still resolves -- adding a channel is
    a registration when its shape differs, never a prerequisite."""
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)

    seen: Dict[str, Any] = {}

    async def fake_resolve(merchant_id: str, handles: Dict[str, str], **kw: Any) -> str:
        seen["evidence"] = kw["evidence"]
        seen["handles"] = handles
        return "cust-1"

    monkeypatch.setattr(workers, "crm_resolve", fake_resolve)

    _run(workers._process_one(_fake_txn(), _event(source="mystery-source")))

    assert seen["evidence"] == "observed"
    assert seen["handles"] == {"phone": "+919999999999"}


def test_a_registered_extractor_overrides_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A9's Shopify lane lands as one dict entry: the pass below it never
    changes."""
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)

    def shopify(payload: Dict[str, Any]) -> Extracted:
        return Extracted(
            handles={"email": payload["contact_email"]},
            facts={"name": payload["contact_name"]},
        )

    monkeypatch.setitem(EXTRACTORS, "shopify", shopify)

    seen: Dict[str, Any] = {}

    async def fake_resolve(merchant_id: str, handles: Dict[str, str], **kw: Any) -> str:
        seen["handles"] = handles
        return "cust-7"

    facts_calls: List[Dict[str, Any]] = []

    async def fake_assert_facts(
        merchant_id: str, customer_id: str, facts: Dict[str, Any], **kw: Any
    ) -> None:
        facts_calls.append(facts)

    monkeypatch.setattr(workers, "crm_resolve", fake_resolve)
    monkeypatch.setattr(workers, "assert_facts", fake_assert_facts)

    event = _event(
        source="shopify",
        payload={"contact_email": "r@example.com", "contact_name": "Rhea"},
    )
    _run(workers._process_one(_fake_txn(), event))

    assert seen["handles"] == {"email": "r@example.com"}
    assert facts_calls == [{"name": "Rhea"}]
    assert fake_accessor.stamped == [("evt-1", "cust-7")]


def test_pretrusted_customer_id_skips_resolution_and_still_stamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-resolved row (crm_mirror pass-through style) skips resolve()
    entirely but still takes its closing stamp."""
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)

    async def fail_resolve(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("resolve() must not run for a pre-resolved row")

    monkeypatch.setattr(workers, "crm_resolve", fail_resolve)

    event = _event(customer_id="cust-42", payload={})
    _run(workers._process_one(_fake_txn(), event))

    assert fake_accessor.stamped == [("evt-1", "cust-42")]
    assert fake_accessor.quarantined == []


def test_pretrusted_customer_id_still_asserts_facts_from_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facts assertion runs for every row that lands on a customer_id,
    regardless of whether that id was pass-through or freshly resolved --
    a mirror's payload can carry profile claims independent of the phone
    handle."""
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)

    fact_calls: List[Tuple[str, str, Dict[str, Any]]] = []

    async def fake_assert_facts(
        merchant_id: str, customer_id: str, facts: Dict[str, Any], **kw: Any
    ) -> None:
        fact_calls.append((merchant_id, customer_id, facts))

    monkeypatch.setattr(workers, "assert_facts", fake_assert_facts)

    event = _event(
        customer_id="cust-42", payload={"customer_name": "Asha", "irrelevant": "x"}
    )
    _run(workers._process_one(_fake_txn(), event))

    assert fact_calls == [("m1", "cust-42", {"name": "Asha"})]


def test_assert_facts_failure_is_swallowed_and_stamp_still_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike a lost enrolment, a lost profile fact breaks nothing
    downstream -- it must log and let the row complete, never fail the row
    forever."""
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)

    async def failing_assert_facts(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("facts db blew up")

    monkeypatch.setattr(workers, "assert_facts", failing_assert_facts)

    event = _event(customer_id="cust-42", payload={"customer_name": "Asha"})
    _run(workers._process_one(_fake_txn(), event))

    assert fake_accessor.stamped == [("evt-1", "cust-42")]


def test_entry_rules_run_per_row_before_that_row_is_stamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consumer sees the row's resolved customer_id and runs INSIDE
    the row's savepoint, before the stamp -- so its write and the stamp
    that marks the row done live or die together."""
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)

    order: List[str] = []
    seen: List[Tuple[str, str]] = []

    async def fake_consume(
        event: RawEvent, customer_id: str, handles: Dict[str, str]
    ) -> None:
        order.append("consume")
        seen.append((event.id, customer_id))

    async def spying_stamp(conn: Any, event_id: str, customer_id: Any) -> None:
        order.append("stamp")
        fake_accessor.stamped.append((event_id, customer_id))

    monkeypatch.setattr(workers, "_consume_attributed_event", fake_consume)
    monkeypatch.setattr(fake_accessor, "stamp_event", spying_stamp)

    _run(workers._process_one(_fake_txn(), _event(customer_id="cust-42", payload={})))

    assert order == ["consume", "stamp"]
    assert seen == [("evt-1", "cust-42")]


def test_entry_rules_never_run_for_a_quarantined_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quarantined row names no customer, so there is nothing for a
    workflow to attribute to."""
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)

    async def fail_consume(
        event: RawEvent, customer_id: str, handles: Dict[str, str]
    ) -> None:
        raise AssertionError("entry rules must not see an unattributed row")

    monkeypatch.setattr(workers, "_consume_attributed_event", fail_consume)

    _run(workers._process_one(_fake_txn(), _event(payload={"lead_id": "l1"})))

    assert fake_accessor.quarantined == [("evt-1", "no_handle")]


def test_a_failing_entry_rule_costs_its_own_row_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sealed contract: a consumer failure raises out of _process_one,
    the row's savepoint rolls back (so it stays pending and retries), and
    the batch's other rows still commit. A batch-level slot would instead
    have re-failed every row in the batch, forever."""
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)

    events = [_event("evt-1", customer_id="c1"), _event("evt-2", customer_id="c2")]

    async def fake_claim(conn: Any, limit: int) -> List[RawEvent]:
        return events

    async def poison_first(
        event: RawEvent, customer_id: str, handles: Dict[str, str]
    ) -> None:
        if event.id == "evt-1":
            raise RuntimeError("workflow rule blew up")

    monkeypatch.setattr(
        fake_accessor, "claim_pending_events", fake_claim, raising=False
    )
    monkeypatch.setattr(workers, "_consume_attributed_event", poison_first)

    result = _run(workers._pass_in_txn(_fake_txn(), 10))

    # evt-1 never reached its stamp; evt-2 completed normally.
    assert fake_accessor.stamped == [("evt-2", "c2")]
    # ...and the claim still reports both rows as work found.
    assert [e.id for e in result] == ["evt-1", "evt-2"]


# --- rollout phase 04 (P2): the claim spends an attempt; poison rows quarantine ---


def test_the_claim_spends_an_attempt_and_returns_it() -> None:
    """The lock is still the claim (FOR UPDATE SKIP LOCKED inside the
    pass's transaction), but the claim now counts against the row — the
    crm_workflow_enrollment shape (058): a crash mid-row counts too, so a
    poison row cannot hide behind never reaching its except."""
    sql, params = claim_pending_events_query(10)
    assert "attempts = attempts + 1" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql and "processed_at IS NULL" in sql
    returning = sql.split("RETURNING", 1)[1]
    assert "attempts" in returning and "payload" in returning
    assert "ORDER BY received_at" in sql  # the pending index's order, kept
    assert params == [10]


def test_decoder_carries_attempts() -> None:
    row = {
        "id": "e1",
        "merchant_id": "m1",
        "source": "shopify",
        "topic": "orders/create",
        "schema_version": "1",
        "external_id": "x",
        "payload": '{"a": 1}',
        "received_at": datetime.now(timezone.utc),
        "occurred_at": None,
        "customer_id": None,
        "attempts": 3,
    }
    assert decode_raw_event(row).attempts == 3


def _poison_pass(
    monkeypatch: pytest.MonkeyPatch, attempts: int, max_attempts: int
) -> _FakeAccessor:
    """One claimed row whose consumer raises deterministically."""
    fake_accessor = _FakeAccessor()
    monkeypatch.setattr(workers, "accessor", fake_accessor)
    monkeypatch.setattr(workers, "CRM_EVENT_MAX_ATTEMPTS", max_attempts)
    event = _event("evt-1", customer_id="c1", attempts=attempts)

    async def fake_claim(conn: Any, limit: int) -> List[RawEvent]:
        return [event]

    async def poison(
        event: RawEvent, customer_id: str, handles: Dict[str, str]
    ) -> None:
        raise RuntimeError("bad live definition")

    monkeypatch.setattr(
        fake_accessor, "claim_pending_events", fake_claim, raising=False
    )
    monkeypatch.setattr(workers, "_consume_attributed_event", poison)
    _run(workers._pass_in_txn(_fake_txn(), 10))
    return fake_accessor


def test_a_row_that_exhausted_its_attempts_is_quarantined_not_retried_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before: a deterministically failing consumer left the row pending
    forever, re-claimed at the head of the queue every poll and re-running
    resolve()/assert_facts() each time. Now the claim that reaches
    CRM_EVENT_MAX_ATTEMPTS quarantines it — outside the rolled-back
    savepoint, as its own statement on the still-valid transaction."""
    fake_accessor = _poison_pass(monkeypatch, attempts=5, max_attempts=5)
    assert len(fake_accessor.quarantined) == 1
    event_id, reason = fake_accessor.quarantined[0]
    assert event_id == "evt-1"
    assert reason.startswith("consumer_error after 5 attempts")
    assert fake_accessor.stamped == []  # quarantine is the terminal stamp


def test_a_row_with_attempts_to_spare_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_accessor = _poison_pass(monkeypatch, attempts=1, max_attempts=5)
    assert fake_accessor.quarantined == [] and fake_accessor.stamped == []


def test_pass_returns_claimed_rows_even_when_every_row_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The return value is the scaffold's "was there work" signal. If it
    only counted successes, a batch that was entirely quarantined would
    look like an empty queue and trigger backoff while the queue is
    actually full."""
    events = [_event("evt-1"), _event("evt-2")]

    async def fake_claim(conn: Any, limit: int) -> List[RawEvent]:
        return events

    async def fake_process_one(conn: Any, event: RawEvent) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(workers.accessor, "claim_pending_events", fake_claim)
    monkeypatch.setattr(workers, "_process_one", fake_process_one)

    result = _run(workers._pass_in_txn(_fake_txn(), 10))

    assert [e.id for e in result] == ["evt-1", "evt-2"]


def test_one_failing_row_does_not_stop_the_rest_of_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each row runs in its own savepoint under the pass's one
    transaction, so an accessor outage on one row leaves the others to
    commit."""
    events = [_event("evt-1"), _event("evt-2")]

    async def fake_claim(conn: Any, limit: int) -> List[RawEvent]:
        return events

    processed: List[str] = []

    async def fake_process_one(conn: Any, event: RawEvent) -> None:
        if event.id == "evt-1":
            raise RuntimeError("boom")
        processed.append(event.id)

    monkeypatch.setattr(workers.accessor, "claim_pending_events", fake_claim)
    monkeypatch.setattr(workers, "_process_one", fake_process_one)

    _run(workers._pass_in_txn(_fake_txn(), 10))

    assert processed == ["evt-2"]


# ---------------------------------------------------------------------------
# The drain loop (app/crm/shared/worker.py)
# ---------------------------------------------------------------------------


def _run_loop(coro: Any, timeout: float = 2.0) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


class _RecordingWaits:
    """Replaces _jittered_wait: records every delay the loop asked for and
    returns instantly, so the tests assert the backoff CURVE without ever
    sleeping."""

    def __init__(self, stop_after: Optional[int] = None) -> None:
        self.delays: List[float] = []
        self._stop_after = stop_after

    async def __call__(self, base: float, stop_event: asyncio.Event) -> None:
        self.delays.append(base)
        if self._stop_after is not None and len(self.delays) >= self._stop_after:
            stop_event.set()


def test_full_batches_loop_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    waits = _RecordingWaits()
    monkeypatch.setattr(worker_mod, "_jittered_wait", waits)

    calls = {"n": 0}
    stop_event = asyncio.Event()

    async def claim(batch: int) -> List[int]:
        calls["n"] += 1
        if calls["n"] >= 5:
            stop_event.set()
        return [1]  # non-empty -> no backoff wait

    async def handle(row: int) -> None:
        pass

    _run_loop(
        run_drain_loop(
            claim, handle, interval=1.0, batch=10, stop_event=stop_event, name="t"
        )
    )

    assert calls["n"] == 5
    assert waits.delays == []  # never waited between full batches


def test_empty_batch_backs_off_doubling_to_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits = _RecordingWaits(stop_after=8)
    monkeypatch.setattr(worker_mod, "_jittered_wait", waits)

    stop_event = asyncio.Event()

    async def claim(batch: int) -> List[int]:
        return []

    async def handle(row: int) -> None:
        pass

    _run_loop(
        run_drain_loop(
            claim, handle, interval=0.5, batch=10, stop_event=stop_event, name="t"
        )
    )

    # doubles from the interval, then pins at the 5s ceiling
    assert waits.delays == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0, 5.0]


def test_finding_work_resets_the_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quiet stretch must not leave a busy worker polling every 5s."""
    waits = _RecordingWaits()
    monkeypatch.setattr(worker_mod, "_jittered_wait", waits)

    stop_event = asyncio.Event()
    script: List[List[int]] = [[], [], [], [1], [], []]
    calls = {"n": 0}

    async def claim(batch: int) -> List[int]:
        i = calls["n"]
        calls["n"] += 1
        if i >= len(script):
            stop_event.set()
            return []
        return script[i]

    async def handle(row: int) -> None:
        pass

    _run_loop(
        run_drain_loop(
            claim, handle, interval=0.5, batch=10, stop_event=stop_event, name="t"
        )
    )

    # 3 empties escalate; the row at index 3 resets; the next two start over
    assert waits.delays[:3] == [0.5, 1.0, 2.0]
    assert waits.delays[3:5] == [0.5, 1.0]


def test_jittered_wait_stays_within_twenty_percent_of_its_base() -> None:
    """The jitter itself, tested directly instead of through the loop."""
    stop_event = asyncio.Event()
    stop_event.set()  # returns immediately; we are asserting the CHOSEN delay

    seen: List[float] = []
    real_wait_for = asyncio.wait_for

    async def spy(aw: Any, timeout: float) -> Any:
        seen.append(timeout)
        return await real_wait_for(aw, timeout=timeout)

    async def scenario() -> None:
        asyncio.wait_for = spy  # type: ignore[assignment]
        try:
            for _ in range(50):
                await worker_mod._jittered_wait(1.0, stop_event)
        finally:
            asyncio.wait_for = real_wait_for  # type: ignore[assignment]

    asyncio.run(scenario())

    assert len(seen) == 50
    assert all(0.8 <= d <= 1.2 for d in seen)
    assert len(set(seen)) > 1  # actually jittered, not a constant


def test_handle_exception_does_not_stop_the_next_row() -> None:
    stop_event = asyncio.Event()
    handled: List[int] = []
    calls = {"n": 0}

    async def claim(batch: int) -> List[int]:
        calls["n"] += 1
        if calls["n"] > 1:
            stop_event.set()
            return []
        return [1, 2, 3]

    async def handle(row: int) -> None:
        if row == 1:
            raise RuntimeError("row 1 blew up")
        handled.append(row)

    _run_loop(
        run_drain_loop(
            claim, handle, interval=0.001, batch=10, stop_event=stop_event, name="t"
        )
    )

    assert handled == [2, 3]


def test_claim_exception_backs_off_and_keeps_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits = _RecordingWaits()
    monkeypatch.setattr(worker_mod, "_jittered_wait", waits)

    stop_event = asyncio.Event()
    calls = {"n": 0}

    async def claim(batch: int) -> List[int]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("db unreachable")
        stop_event.set()
        return []

    async def handle(row: int) -> None:
        pass

    _run_loop(
        run_drain_loop(
            claim, handle, interval=0.5, batch=10, stop_event=stop_event, name="t"
        )
    )

    assert calls["n"] == 3
    # a failed claim backs off on the same curve as an empty one
    assert waits.delays == [0.5, 1.0, 2.0]


def test_stop_event_set_before_start_exits_without_claiming() -> None:
    stop_event = asyncio.Event()
    stop_event.set()
    claimed = {"called": False}

    async def claim(batch: int) -> List[int]:
        claimed["called"] = True
        return []

    async def handle(row: int) -> None:
        pass

    _run_loop(
        run_drain_loop(
            claim, handle, interval=5.0, batch=10, stop_event=stop_event, name="t"
        )
    )

    assert claimed["called"] is False


def test_stop_event_mid_batch_stops_before_the_next_row() -> None:
    """SIGTERM during a batch: the in-flight row finishes, the rest are
    left claimed-but-pending for the next pod."""
    stop_event = asyncio.Event()
    handled: List[int] = []

    async def claim(batch: int) -> List[int]:
        return [1, 2, 3]

    async def handle(row: int) -> None:
        handled.append(row)
        if row == 2:
            stop_event.set()

    _run_loop(
        run_drain_loop(
            claim, handle, interval=0.001, batch=10, stop_event=stop_event, name="t"
        )
    )

    assert handled == [1, 2]


def test_stop_event_during_a_wait_ends_the_loop() -> None:
    """The wait must be interruptible: a shutdown cannot sit through a
    full 5s backoff."""
    stop_event = asyncio.Event()

    async def claim(batch: int) -> List[int]:
        return []

    async def handle(row: int) -> None:
        pass

    async def scenario() -> None:
        task = asyncio.create_task(
            run_drain_loop(
                claim, handle, interval=30.0, batch=10, stop_event=stop_event, name="t"
            )
        )
        await asyncio.sleep(0)  # let it reach the wait
        stop_event.set()
        await task  # would hang for 30s if the wait ignored the event

    _run_loop(scenario())


def test_idle_worker_still_heartbeats(monkeypatch: pytest.MonkeyPatch) -> None:
    """A silent worker and a dead worker look identical in logs; the
    heartbeat is what tells them apart, so it must fire with no rows."""
    waits = _RecordingWaits(stop_after=3)
    monkeypatch.setattr(worker_mod, "_jittered_wait", waits)
    monkeypatch.setattr(worker_mod, "CRM_WORKER_HEARTBEAT", 0.0)

    beats: List[str] = []
    monkeypatch.setattr(
        worker_mod.logger, "info", lambda msg, *a, **k: beats.append(str(msg))
    )

    stop_event = asyncio.Event()

    async def claim(batch: int) -> List[int]:
        return []

    async def handle(row: int) -> None:
        pass

    _run_loop(
        run_drain_loop(
            claim, handle, interval=0.5, batch=10, stop_event=stop_event, name="ew"
        )
    )

    assert any("ew: alive" in b for b in beats)


def test_heartbeat_counts_rows_since_the_last_beat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_mod, "CRM_WORKER_HEARTBEAT", 0.0)

    beats: List[str] = []
    monkeypatch.setattr(
        worker_mod.logger, "info", lambda msg, *a, **k: beats.append(str(msg))
    )

    stop_event = asyncio.Event()
    calls = {"n": 0}

    async def claim(batch: int) -> List[int]:
        calls["n"] += 1
        if calls["n"] > 2:
            stop_event.set()
            return []
        return [1, 2, 3]

    async def handle(row: int) -> None:
        pass

    _run_loop(
        run_drain_loop(
            claim, handle, interval=0.001, batch=10, stop_event=stop_event, name="ew"
        )
    )

    counted: List[Tuple[str, int]] = [
        (b, int(b.split("alive, ")[1].split(" rows")[0])) for b in beats if "alive" in b
    ]
    assert counted, "expected at least one heartbeat"
    # first beat precedes any work; a later one reports the batch just done
    assert any(n == 3 for _, n in counted)


# --- the Shopify extractor: a pipe's letter, read at the belt ---


def test_shopify_extractor_reads_the_nested_customer_phone() -> None:
    # The relay forwards Shopify's body unopened, so the phone arrives
    # nested and the top-level one is usually null.
    extracted = shopify_extractor.extract(
        {
            "phone": None,
            "customer": {
                "first_name": "Priya",
                "last_name": "Sharma",
                "phone": "+91 98765 43210",
            },
            "shipping_address": {"phone": "9999999999"},
        }
    )
    assert extracted.handles["phone"] == "+919876543210"  # normalized
    assert extracted.facts == {"name": "Priya Sharma"}


def test_shopify_extractor_falls_back_to_the_shipping_contact() -> None:
    # A guest checkout carries no customer object at all.
    extracted = shopify_extractor.extract(
        {
            "shipping_address": {
                "first_name": "Rohan",
                "last_name": "Mehta",
                "phone": "9876543210",
            }
        }
    )
    assert extracted.handles["phone"] == "+919876543210"
    assert extracted.facts == {"name": "Rohan Mehta"}


def test_shopify_extractor_never_invents_a_name() -> None:
    # A placeholder would reach assert_facts as a real name claim and
    # overwrite what we actually know. Absent is absent.
    extracted = shopify_extractor.extract({"customer": {"phone": "9876543210"}})
    assert extracted.facts == {}


def test_shopify_extractor_skips_an_unusable_phone() -> None:
    # normalize_phone returns None rather than writing a bad handle; with
    # nothing usable the row quarantines no_handle and stays replayable.
    extracted = shopify_extractor.extract({"customer": {"phone": "n/a"}})
    assert "phone" not in extracted.handles


def test_shopify_extractor_takes_the_email_too() -> None:
    extracted = shopify_extractor.extract(
        {"customer": {"email": "  Priya@Example.COM  ", "phone": "9876543210"}}
    )
    assert extracted.handles["email"] == "priya@example.com"


def test_shopify_source_is_registered() -> None:
    # Without the registration a raw Shopify body falls to _extract_flat,
    # which looks for a top-level customer_mobile_number that is not
    # there — every order would quarantine no_handle.
    assert EXTRACTORS["shopify"] is shopify_extractor.extract


def test_shopify_extractor_takes_the_customer_id_handle() -> None:
    # The corpus's own example for this extractor is
    # shopify/checkouts.create -> {phone, email, shopify_customer_id}.
    extracted = shopify_extractor.extract(
        {"customer": {"id": 77, "phone": "9876543210"}}
    )
    assert extracted.handles["shopify_customer_id"] == "77"


def test_shopify_extractor_resolves_a_phoneless_checkout_frame() -> None:
    # The checkouts/update stream's early frames carry no phone — she
    # types it later — but a signed-in shopper's customer id is there
    # from the first frame. Without this handle those frames quarantine
    # no_handle; with it they attribute to the person.
    extracted = shopify_extractor.extract(
        {"token": "chk-88412", "phone": None, "customer": {"id": 77}}
    )
    assert extracted.handles == {"shopify_customer_id": "77"}
    assert extracted.facts == {}


def test_shopify_extractor_omits_the_id_handle_for_a_guest() -> None:
    # A guest checkout carries no customer object at all.
    extracted = shopify_extractor.extract({"shipping_address": {"phone": "9876543210"}})
    assert "shopify_customer_id" not in extracted.handles


def test_shopify_extractor_reads_the_default_address() -> None:
    # design/ingest-doors names customer.default_address.phone as this
    # extractor's mapping: a returning shopper's number often lives only
    # there, with the customer object itself carrying none.
    extracted = shopify_extractor.extract(
        {
            "customer": {
                "id": 77,
                "phone": None,
                "default_address": {
                    "phone": "9876543210",
                    "first_name": "Priya",
                    "last_name": "Sharma",
                },
            }
        }
    )
    assert extracted.handles["phone"] == "+919876543210"
    assert extracted.facts == {"name": "Priya Sharma"}


def test_shopify_extractor_prefers_the_customer_phone_over_default_address() -> None:
    # Probe order is the law: most specific first.
    extracted = shopify_extractor.extract(
        {
            "customer": {
                "phone": "+919999999999",
                "default_address": {"phone": "9876543210"},
            }
        }
    )
    assert extracted.handles["phone"] == "+919999999999"


def test_pass_hands_the_extractors_handles_to_the_consumer() -> None:
    # THE divergence this closes: the extractor searches four places for a
    # phone, the consumer's own payload reader searches three — and the
    # canonical Shopify order keeps its number in customer.default_address,
    # which only the extractor knows. Resolved-then-parked was the result:
    # identity found her, the call node could not.
    import asyncio

    seen: Dict[str, Any] = {}

    async def fake_resolve(merchant_id, handles, evidence, source):  # type: ignore[no-untyped-def]
        return "cus-1"

    async def fake_facts(*a: Any, **k: Any) -> None:
        return None

    async def fake_consume(event, customer_id, handles):  # type: ignore[no-untyped-def]
        seen["handles"] = handles

    workers.crm_resolve = fake_resolve  # type: ignore[assignment]
    workers.assert_facts = fake_facts  # type: ignore[assignment]
    record_consumers._CONSUMERS = [fake_consume]

    async def fake_stamp(*a: Any, **k: Any) -> None:
        return None

    workers.accessor.stamp_event = fake_stamp  # type: ignore[assignment]

    event = RawEvent(
        id="e1",
        merchant_id="m1",
        source="shopify",
        topic="orders/create",
        schema_version="1",
        external_id="orders/create:1",
        payload={
            "phone": None,
            "customer": {"id": 7, "default_address": {"phone": "9876543210"}},
        },
        received_at=datetime.now(timezone.utc),
    )
    asyncio.run(workers._process_one(None, event))  # type: ignore[arg-type]

    # the number the sends will dial is the number identity resolved on
    assert seen["handles"]["phone"] == "+919876543210"

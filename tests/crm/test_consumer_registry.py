"""The spine consumer registry: record owns the WHEN, worker_main owns the
WHO, and the import arrow only ever points subscriber -> record."""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

import app.crm.record.consumers as record_consumers
import app.crm.record.workers as workers
from app.crm.record.consumers import consumers, register_consumer
from app.crm.record.schemas import RawEvent


async def _consumer_a(event: RawEvent, customer_id: str, handles: Any) -> None:
    return None


def test_register_is_idempotent() -> None:
    # Worker imports can run twice (tests, reload); the same function must
    # never end up delivering every event twice.
    before = consumers()
    register_consumer(_consumer_a)
    register_consumer(_consumer_a)
    added = [c for c in consumers() if c is _consumer_a]
    assert len(added) == 1
    record_consumers._CONSUMERS = [
        c for c in record_consumers._CONSUMERS if c is not _consumer_a
    ]
    assert consumers() == before


def test_consumers_returns_a_copy() -> None:
    # Mutating the returned list must not edit the registry.
    snapshot = consumers()
    snapshot.append(_consumer_a)
    assert _consumer_a not in consumers()


def test_the_pass_runs_every_registered_consumer_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Registration order is execution order: entry rules first today;
    # segments and the A13 transactional-send consumer join behind them
    # with zero edits in the pass.
    ran: List[str] = []

    async def first(event: RawEvent, customer_id: str, handles: Any) -> None:
        ran.append(f"first:{customer_id}:{(handles or {}).get('phone')}")

    async def second(event: RawEvent, customer_id: str, handles: Any) -> None:
        ran.append(f"second:{customer_id}")

    monkeypatch.setattr(record_consumers, "_CONSUMERS", [first, second])
    event = RawEvent(
        id="e1",
        merchant_id="m1",
        source="lead-api",
        topic="t",
        schema_version="1",
        external_id="x",
        payload={},
        received_at=datetime.now(timezone.utc),
    )
    asyncio.run(
        workers._consume_attributed_event(event, "cust-1", {"phone": "+911234567890"})
    )
    assert ran == ["first:cust-1:+911234567890", "second:cust-1"]


def test_worker_main_registers_the_entry_consumer() -> None:
    # The composition root fills the slot: importing worker_main is what
    # wires outreach's entry rules onto the spine.
    import app.crm.worker_main  # noqa: F401  (registration is an import effect)
    from app.crm.outreach.contracts import consume_attributed_event

    assert consume_attributed_event in consumers()


def test_record_imports_no_subscriber() -> None:
    # The structural inversion itself, greppable: nothing under record/
    # imports outreach (or any other subscriber module). Rule 12 enforces
    # this in CI over the real tree; this pins it from the test suite too.
    import pathlib

    record_dir = pathlib.Path(workers.__file__).parent
    offenders: Dict[str, str] = {}
    for py in record_dir.rglob("*.py"):
        text = py.read_text()
        for needle in ("app.crm.outreach", "app.crm.connectivity"):
            if needle in text:
                offenders[str(py)] = needle
    assert offenders == {}

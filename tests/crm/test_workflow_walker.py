"""The walker's writes are conditional on the lease they were claimed
under (P1, rollout phase 03).

claim_due_runs pushes wake_at one lease window and RETURNS the row, so the
decoded run's wake_at IS the claim's token. Every event-side writer moves
wake_at (a reply sets now(), a repeat patch slides it), so a walker write
that still carries the leased value is a write nobody raced; one that does
not match zero rows. On that miss the walker defers — the lease already
re-arms the run, and the next claim re-reads it WITH the reply and takes
the right branch. Before this, advance_run overwrote context and wake_at
unconditionally and the timeout path could clobber a reply that landed
mid-visit.

Exercised against the private _advance/walk_run with a fake accessor slice,
the same shape test_event_worker.py uses for the pass."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import pytest

import app.crm.outreach.walker as walker
from app.crm.outreach.nodes import NodeParked
from app.crm.outreach.schemas import EnrollmentRun, Workflow, WorkflowDefinition

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
LEASE = NOW + timedelta(seconds=300)

_TWO_WAITS = {
    "entry": {"topic": "checkout.initiated"},
    "nodes": [
        {"id": "wait-30m", "type": "wait", "minutes": 30},
        {"id": "wait-1d", "type": "wait", "minutes": 1440},
    ],
    "edges": [["wait-30m", "wait-1d"]],
    "goal": {"topics": ["order.placed"]},
}
_ONE_WAIT = {**_TWO_WAITS, "nodes": _TWO_WAITS["nodes"][:1], "edges": []}


def _run(wake_at: Optional[datetime] = LEASE, attempts: int = 1) -> EnrollmentRun:
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=uuid4(),
        workflow_version=1,
        customer_id=uuid4(),
        status="waiting",
        current_node="wait-30m",
        wake_at=wake_at,
        entered_at=NOW - timedelta(minutes=31),
        exited_at=None,
        exit_reason=None,
        context={"phone": "+919876543210"},
        enrollment_key="c-1",
        attempts=attempts,
        last_error=None,
    )


class _Writes:
    """The accessor slice the walker writes through. ``matched`` is the
    CAS answer every write returns; ``calls`` records what was asked."""

    def __init__(self, matched: bool, definition: Dict[str, Any]) -> None:
        self.matched = matched
        self.definition = definition
        self.calls: List[Tuple[str, Tuple[Any, ...]]] = []

    async def get_workflow(self, merchant_id: str, workflow_id: str) -> Workflow:
        return Workflow(
            id=uuid4(),
            merchant_id=merchant_id,
            name="plan",
            status="live",
            version=1,
            created_by=None,
            created_at=NOW,
            updated_at=NOW,
            definition=self.definition,
            draft=None,
        )

    async def advance_run(self, *args: Any) -> bool:
        self.calls.append(("advance", args))
        return self.matched

    async def exit_run(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append(("exit", args + tuple(kwargs.values())))
        return self.matched

    async def park_run(self, *args: Any) -> bool:
        self.calls.append(("park", args))
        return self.matched

    async def record_run_error(self, *args: Any) -> bool:
        self.calls.append(("retry", args))
        return self.matched


@pytest.fixture
def no_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(walker, "customer_has_event", never)


def _advance(writes: _Writes, run: EnrollmentRun) -> None:
    definition = WorkflowDefinition.model_validate(writes.definition)
    asyncio.run(walker._advance(run, definition, LEASE))


def test_advance_carries_the_lease_it_was_claimed_under(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    writes = _Writes(matched=True, definition=_TWO_WAITS)
    monkeypatch.setattr(walker, "accessor", writes)
    run = _run()
    _advance(writes, run)
    ((verb, args),) = writes.calls
    assert verb == "advance"
    run_id, node, wake, context, lease = args
    assert (run_id, node, lease) == (str(run.id), "wait-1d", LEASE)
    assert context == run.context
    assert (
        timedelta(minutes=1439)
        < wake - datetime.now(timezone.utc)
        < timedelta(minutes=1441)
    )


def test_a_missed_cas_on_advance_defers_without_raising_or_exiting(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """The run changed under the lease (a reply landed mid-visit). The
    walker neither raises nor writes anything else — the next claim
    re-reads the run with the reply in it."""
    writes = _Writes(matched=False, definition=_TWO_WAITS)
    monkeypatch.setattr(walker, "accessor", writes)
    _advance(writes, _run())
    assert [verb for verb, _ in writes.calls] == ["advance"]


def test_a_missed_cas_on_exit_defers_too(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    writes = _Writes(matched=False, definition=_ONE_WAIT)
    monkeypatch.setattr(walker, "accessor", writes)
    run = _run()
    _advance(writes, run)
    ((verb, args),) = writes.calls
    assert verb == "exit"
    assert args[0] == str(run.id) and args[1] == "completed" and LEASE in args


def test_a_park_under_a_stale_lease_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(matched=False, definition=_TWO_WAITS)
    monkeypatch.setattr(walker, "accessor", writes)

    async def parked(*args: Any) -> None:
        raise NodeParked("template gone")

    monkeypatch.setattr(walker, "_advance", parked)
    asyncio.run(walker.walk_run(_run()))  # must not raise
    ((verb, args),) = writes.calls
    assert verb == "park" and args[-1] == LEASE


def test_a_retry_under_a_stale_lease_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(matched=False, definition=_TWO_WAITS)
    monkeypatch.setattr(walker, "accessor", writes)

    async def flaky(*args: Any) -> None:
        raise RuntimeError("provider hiccup")

    monkeypatch.setattr(walker, "_advance", flaky)
    asyncio.run(walker.walk_run(_run(attempts=1)))
    ((verb, args),) = writes.calls
    assert verb == "retry" and args[-1] == LEASE


def test_walk_run_never_writes_without_a_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claimed run always carries its lease (the claim wrote it; waiting
    rows have wake_at NOT NULL). Without one there is no token to write
    under, and a blind overwrite is exactly the bug this phase closes."""
    writes = _Writes(matched=True, definition=_TWO_WAITS)
    monkeypatch.setattr(walker, "accessor", writes)
    asyncio.run(walker.walk_run(_run(wake_at=None)))
    assert writes.calls == []

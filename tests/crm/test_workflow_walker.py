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

    def __init__(
        self,
        matched: bool,
        definition: Dict[str, Any],
        versions: Optional[Dict[int, Dict[str, Any]]] = None,
    ) -> None:
        self.matched = matched
        self.definition = definition
        # The version rows (phase 12): what get_definition answers by pin.
        # Default: the live document IS v1, the version _run() enters under.
        self.versions = versions if versions is not None else {1: definition}
        self.definition_reads: List[Tuple[str, str, int]] = []
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

    async def get_definition(
        self, merchant_id: str, workflow_id: str, version: int
    ) -> Optional[Dict[str, Any]]:
        self.definition_reads.append((merchant_id, workflow_id, version))
        return self.versions.get(version)

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


# --- rollout phase 06: the fire-time goal re-check judges tiers keyed-first ---

_TIERED = {
    **_TWO_WAITS,
    "goals": [
        {
            "topics": ["orders/create"],
            "key": {"event": "cart_token", "run": "cart_token"},
            "exit_reason": "goal_met",
        },
        {"topics": ["orders/create"], "exit_reason": "converted_elsewhere"},
    ],
}
del _TIERED["goal"]
ENTERED_EVENT_AT = NOW - timedelta(hours=2)


def _tiered_run() -> EnrollmentRun:
    run = _run()
    run.context = {
        "phone": "+919876543210",
        "cart_token": "chk-1",
        "entered_event_at": ENTERED_EVENT_AT.isoformat(),
    }
    return run


def _goal_recheck(
    monkeypatch: pytest.MonkeyPatch, answers: Dict[Optional[Tuple[str, str]], bool]
) -> List[Tuple[Any, ...]]:
    asked: List[Tuple[Any, ...]] = []

    async def customer_has_event(
        merchant_id: str,
        customer_id: str,
        topics: List[str],
        since: datetime,
        where: Optional[Tuple[str, str]] = None,
    ) -> bool:
        asked.append((tuple(topics), since, where))
        return answers.get(where, False)

    monkeypatch.setattr(walker, "customer_has_event", customer_has_event)
    return asked


def test_this_cart_recovered_exits_goal_met(monkeypatch: pytest.MonkeyPatch) -> None:
    writes = _Writes(matched=True, definition=_TIERED)
    monkeypatch.setattr(walker, "accessor", writes)
    asked = _goal_recheck(monkeypatch, {("cart_token", "chk-1"): True})
    _advance(writes, _tiered_run())
    ((verb, args),) = writes.calls
    assert verb == "exit" and args[1] == "goal_met"
    # keyed tier asked first, against the ENTRY EVENT's time (G7)
    assert asked[0] == (("orders/create",), ENTERED_EVENT_AT, ("cart_token", "chk-1"))


def test_another_order_exits_converted_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(matched=True, definition=_TIERED)
    monkeypatch.setattr(walker, "accessor", writes)
    asked = _goal_recheck(monkeypatch, {None: True})
    _advance(writes, _tiered_run())
    ((verb, args),) = writes.calls
    assert verb == "exit" and args[1] == "converted_elsewhere"
    assert [where for _, _, where in asked] == [("cart_token", "chk-1"), None]


def test_a_run_without_the_key_falls_back_to_the_row_time_and_the_unkeyed_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older row (no entered_event_at) compares against entered_at, and
    a run whose context lacks the key field cannot match a keyed tier."""
    writes = _Writes(matched=True, definition=_TIERED)
    monkeypatch.setattr(walker, "accessor", writes)
    asked = _goal_recheck(monkeypatch, {})
    run = _run()
    _advance(writes, run)
    assert asked == [(("orders/create",), run.entered_at, None)]
    assert [verb for verb, _ in writes.calls] == ["advance"]  # no goal -> moved on


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


# --- rollout phase 12: the walker executes the version a run entered under ---

# v3: wait-30m -> wait-1d (the document above). v4 re-routes the second
# square: a run standing on wait-30m must go where ITS version says.
_V3 = _TWO_WAITS
_V4 = {
    **_TWO_WAITS,
    "nodes": [
        {"id": "wait-30m", "type": "wait", "minutes": 30},
        {"id": "wait-2h", "type": "wait", "minutes": 120},
    ],
    "edges": [["wait-30m", "wait-2h"]],
}


def _pinned_run(version: int) -> EnrollmentRun:
    run = _run()
    run.workflow_version = version
    return run


@pytest.fixture
def cold_cache() -> None:
    """The definition cache is process-wide and never invalidates
    (versions are immutable) — each test starts it empty."""
    walker._definitions.clear()


def test_the_walker_executes_the_version_the_run_entered_under(
    monkeypatch: pytest.MonkeyPatch, no_goal: None, cold_cache: None
) -> None:
    """The live row already carries v4 (wait-30m -> wait-2h); the run
    entered under v3 (wait-30m -> wait-1d) and finishes on v3. The pin is
    read by (merchant, workflow, version), never from crm_workflow."""
    writes = _Writes(matched=True, definition=_V4, versions={3: _V3, 4: _V4})
    monkeypatch.setattr(walker, "accessor", writes)
    run = _pinned_run(3)
    asyncio.run(walker.walk_run(run))
    ((verb, args),) = writes.calls
    assert verb == "advance" and args[1] == "wait-1d"
    assert writes.definition_reads == [("m1", str(run.workflow_id), 3)]


def test_a_missing_version_parks_the_run_honestly(
    monkeypatch: pytest.MonkeyPatch, no_goal: None, cold_cache: None
) -> None:
    """No version row for the pin: an honest park under the lease, never
    a silent fallback to the live document (which would execute a plan
    the run did not enter under)."""
    writes = _Writes(matched=True, definition=_V4, versions={4: _V4})
    monkeypatch.setattr(walker, "accessor", writes)
    run = _pinned_run(3)
    asyncio.run(walker.walk_run(run))
    ((verb, args),) = writes.calls
    assert verb == "park"
    assert args[0] == str(run.id) and args[-1] == LEASE
    assert "definition v3 missing" in args[1]


def test_a_pinned_version_is_read_once_per_process(
    monkeypatch: pytest.MonkeyPatch, no_goal: None, cold_cache: None
) -> None:
    """Versions are immutable (064's trigger), so the second claim of any
    run on the same (workflow, version) is served from the walker's cache
    without a read."""
    writes = _Writes(matched=True, definition=_V3, versions={3: _V3})
    monkeypatch.setattr(walker, "accessor", writes)
    first = _pinned_run(3)
    second = _pinned_run(3)
    second.workflow_id = first.workflow_id
    asyncio.run(walker.walk_run(first))
    asyncio.run(walker.walk_run(second))
    assert [verb for verb, _ in writes.calls] == ["advance", "advance"]
    assert len(writes.definition_reads) == 1


def test_the_cache_is_bounded_and_evicts_the_least_recently_used(
    monkeypatch: pytest.MonkeyPatch, cold_cache: None
) -> None:
    monkeypatch.setattr(walker, "_DEFINITION_CACHE_SIZE", 2)
    writes = _Writes(matched=True, definition=_V3, versions={1: _V3, 2: _V3, 3: _V3})
    monkeypatch.setattr(walker, "accessor", writes)
    runs = [_pinned_run(version) for version in (1, 2, 3)]
    for run in runs[1:]:
        run.workflow_id = runs[0].workflow_id

    async def read_in_order() -> None:
        await walker._definition_for(runs[0])  # v1: miss
        await walker._definition_for(runs[1])  # v2: miss
        await walker._definition_for(runs[0])  # v1: hit, now most recent
        await walker._definition_for(runs[2])  # v3: miss, evicts v2
        await walker._definition_for(runs[1])  # v2: miss again

    asyncio.run(read_in_order())
    assert [version for _, _, version in writes.definition_reads] == [1, 2, 3, 2]

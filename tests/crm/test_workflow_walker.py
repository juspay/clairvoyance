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

import app.crm.outreach.definitions as definitions
import app.crm.outreach.walker as walker
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.schemas import EnrollmentRun, Workflow, WorkflowDefinition
from tests.crm.doubles import patch_accessors

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
LEASE = NOW + timedelta(seconds=300)


class _Frozen(datetime):
    """`datetime` with `now()` pinned to NOW.

    A subclass because the stdlib type is immutable, and a subclass rather
    than a stand-in so the walker's arithmetic behaves as it really does.
    """

    @classmethod
    def now(cls, tz: Optional[timezone] = None) -> datetime:  # type: ignore[override]
        return NOW


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixtures are dated; the code under test is not.

    NOW is a fixed 3 September, but walk_run asks the REAL clock for
    `now - entered_at > max_age_days` (default 7). On the 10th every fixture
    crossed its expiry and exited `timed_out` — nine tests failed on a date.
    """
    monkeypatch.setattr(walker, "datetime", _Frozen)


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
        # The T26 flush rides as keyword arguments, so `args` keeps the
        # positional shape every assertion below already unpacks.
        self.flushes: List[Dict[str, Any]] = []

    async def get_workflow(self, merchant_id: str, workflow_id: str) -> Workflow:
        return Workflow(
            id=uuid4(),
            merchant_id=merchant_id,
            name="plan",
            status="live",
            version=1,
            created_by=None,
            updated_by=None,
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

    async def advance_run(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append(("advance", args))
        self.flushes.append(kwargs)
        return self.matched

    async def exit_run(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append(("exit", args + tuple(kwargs.values())))
        self.flushes.append(kwargs)
        return self.matched

    async def park_run(self, *args: Any) -> bool:
        self.calls.append(("park", args))
        return self.matched

    async def record_run_error(self, *args: Any) -> bool:
        self.calls.append(("retry", args))
        return self.matched


def _install(monkeypatch: pytest.MonkeyPatch, writes: _Writes) -> None:
    """The walker writes through its accessor; the pinned document comes
    through definitions.py's (phase 13) — one fake serves both doors."""
    patch_accessors(monkeypatch, walker, writes)
    patch_accessors(monkeypatch, definitions, writes)


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
    _install(monkeypatch, writes)
    run = _run()
    _advance(writes, run)
    ((verb, args),) = writes.calls
    assert verb == "advance"
    run_id, node, wake, context, lease = args
    assert (run_id, node, lease) == (str(run.id), "wait-1d", LEASE)
    assert context == run.context
    # Against the frozen clock, so exactly — the old window either side only
    # existed to absorb drift between two real now() calls.
    assert wake == NOW + timedelta(minutes=1440)


def test_a_missed_cas_on_advance_defers_without_raising_or_exiting(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """The run changed under the lease (a reply landed mid-visit). The
    walker neither raises nor writes anything else — the next claim
    re-reads the run with the reply in it."""
    writes = _Writes(matched=False, definition=_TWO_WAITS)
    _install(monkeypatch, writes)
    _advance(writes, _run())
    assert [verb for verb, _ in writes.calls] == ["advance"]


def test_a_missed_cas_on_exit_defers_too(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    writes = _Writes(matched=False, definition=_ONE_WAIT)
    _install(monkeypatch, writes)
    run = _run()
    _advance(writes, run)
    ((verb, args),) = writes.calls
    assert verb == "exit"
    assert args[0] == str(run.id) and args[1] == "completed" and LEASE in args


def test_a_park_under_a_stale_lease_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(matched=False, definition=_TWO_WAITS)
    _install(monkeypatch, writes)

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
    _install(monkeypatch, writes)

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
    _install(monkeypatch, writes)
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
    _install(monkeypatch, writes)
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
    _install(monkeypatch, writes)
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
    _install(monkeypatch, writes)
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
    definitions._definitions.clear()


def test_the_walker_executes_the_version_the_run_entered_under(
    monkeypatch: pytest.MonkeyPatch, no_goal: None, cold_cache: None
) -> None:
    """The live row already carries v4 (wait-30m -> wait-2h); the run
    entered under v3 (wait-30m -> wait-1d) and finishes on v3. The pin is
    read by (merchant, workflow, version), never from crm_workflow."""
    writes = _Writes(matched=True, definition=_V4, versions={3: _V3, 4: _V4})
    _install(monkeypatch, writes)
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
    _install(monkeypatch, writes)
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
    _install(monkeypatch, writes)
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
    monkeypatch.setattr(definitions, "_DEFINITION_CACHE_SIZE", 2)
    writes = _Writes(matched=True, definition=_V3, versions={1: _V3, 2: _V3, 3: _V3})
    _install(monkeypatch, writes)
    runs = [_pinned_run(version) for version in (1, 2, 3)]
    for run in runs[1:]:
        run.workflow_id = runs[0].workflow_id

    async def read_in_order() -> None:
        await definitions.definition_for(runs[0])  # v1: miss
        await definitions.definition_for(runs[1])  # v2: miss
        await definitions.definition_for(runs[0])  # v1: hit, now most recent
        await definitions.definition_for(runs[2])  # v3: miss, evicts v2
        await definitions.definition_for(runs[1])  # v2: miss again

    asyncio.run(read_in_order())
    assert [version for _, _, version in writes.definition_reads] == [1, 2, 3, 2]


# --- rollout phase 15: a reply is cleared when the token leaves the square ---

_ASK_TWICE = {
    "entry": {"topic": "orders/create"},
    "nodes": [
        {
            "id": "ask",
            "type": "wait",
            "topics": ["button.reply"],
            "key": "button_id",
            "minutes": 60,
        },
        {"id": "wait-1d", "type": "wait", "minutes": 1440},
    ],
    "edges": [["ask", "wait-1d", "YES"], ["ask", "wait-1d", "timeout"]],
    "goal": {"topics": ["order.confirmed"]},
}


def test_advancing_off_a_listening_square_clears_its_reply(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """Once a door may start a run anywhere, a square can be revisited;
    a stale reply_<node> left in context would resolve the revisit at
    once on an old answer (§14.1 prerequisite 4)."""
    writes = _Writes(matched=True, definition=_ASK_TWICE)
    _install(monkeypatch, writes)
    run = _run()
    run.current_node = "ask"
    run.context = {"phone": "+919876543210", "reply_ask": "YES", "order_id": "O-1"}
    _advance(writes, run)
    ((verb, args),) = writes.calls
    assert verb == "advance" and args[1] == "wait-1d"
    written = args[3]
    assert "reply_ask" not in written and written["order_id"] == "O-1"


# --- the condition node (enh A/01): branch, then keep walking ----------------

_CONDITION_BOARD = {
    "entry": {"topic": "checkout.initiated"},
    "nodes": [
        {"id": "wait-30m", "type": "wait", "minutes": 30},
        {
            "id": "decide",
            "type": "condition",
            "rules": [
                {
                    "on": "big",
                    "if": [{"field": "context.cart_value", "op": ">=", "value": 5000}],
                }
            ],
        },
        {"id": "rescue-call", "type": "call", "template_id": "tpl-1"},
        {"id": "wa-nudge", "type": "send", "channel": "whatsapp", "template": "t"},
        {"id": "wait-1d", "type": "wait", "minutes": 1440},
    ],
    "edges": [
        ["wait-30m", "decide"],
        ["decide", "rescue-call", "big"],
        ["decide", "wa-nudge", "else"],
        ["rescue-call", "wait-1d"],
        ["wa-nudge", "wait-1d"],
    ],
    "goal": {"topics": ["order.placed"]},
    "purpose_key": "utility",
}


def _quiet_actions(monkeypatch: pytest.MonkeyPatch, fired: List[str]) -> None:
    """The action squares record their name instead of reaching a machine."""
    import app.crm.outreach.nodes as nodes

    async def call(run: Any, node: Any, definition: Any) -> Dict[str, Any]:
        fired.append("call")
        return {"lead_rescue-call": "lead-1"}

    async def send(run: Any, node: Any, definition: Any) -> Dict[str, Any]:
        fired.append("send")
        return {"message_wa-nudge": "msg-1"}

    monkeypatch.setitem(
        nodes.NODE_TYPES,
        "call",
        nodes.NODE_TYPES["call"].__class__(
            validate=nodes.NODE_TYPES["call"].validate, execute=call, is_wait=False
        ),
    )
    monkeypatch.setitem(
        nodes.NODE_TYPES,
        "send",
        nodes.NODE_TYPES["send"].__class__(
            validate=nodes.NODE_TYPES["send"].validate, execute=send, is_wait=False
        ),
    )


@pytest.mark.parametrize(
    "cart_value, expect",
    [(6000, "call"), (1000, "send"), (None, "send")],
    ids=["big->call", "small->send", "missing->else"],
)
def test_a_condition_branches_and_the_visit_continues_to_the_next_wait(
    monkeypatch: pytest.MonkeyPatch, no_goal: None, cart_value: Any, expect: str
) -> None:
    """decide is not a wait: the walker evaluates it, takes the labelled
    edge, runs the action square and only THEN writes — one advance, onto
    wait-1d, with the condition's reply cleared like a listening square's."""
    fired: List[str] = []
    _quiet_actions(monkeypatch, fired)
    writes = _Writes(matched=True, definition=_CONDITION_BOARD)
    _install(monkeypatch, writes)
    run = _run()
    run.current_node = "decide"
    run.context = {"phone": "+919876543210"}
    if cart_value is not None:
        run.context["cart_value"] = cart_value
    _advance(writes, run)
    assert fired == [expect]
    ((verb, args),) = writes.calls
    assert verb == "advance" and args[1] == "wait-1d"
    assert "reply_decide" not in args[3]


# --- the calling window: the timer waits for the hours, a letter never does --

# NOW is 17:30 IST. _CLOSED has shut for the day; _OPEN is still open.
_CLOSED = {"opens": "09:00", "closes": "17:00", "timezone": "Asia/Kolkata"}
_OPEN = {"opens": "09:00", "closes": "18:00", "timezone": "Asia/Kolkata"}
# 09:00 IST on the next day, in UTC.
_NEXT_OPENING = datetime(2026, 9, 4, 3, 30, tzinfo=timezone.utc)


def _windowed(hours: Dict[str, str]) -> Dict[str, Any]:
    """The Flipkart ladder's shape: quiet -> call -> gap, both waits held to
    the hours; a letter on quiet goes to listen, which has no window."""
    listening = {"type": "wait", "topics": ["checkout.updated"], "key": "$topic"}
    return {
        "entry": {"topic": "checkout.initiated"},
        "nodes": [
            {"id": "quiet", "minutes": 15, "window": hours, **listening},
            {"id": "call-1", "type": "call", "template_id": "tpl-1"},
            {"id": "gap", "minutes": 30, "window": hours, **listening},
            {"id": "listen", "minutes": 1440, **listening},
        ],
        "edges": [
            ["quiet", "call-1", "timeout"],
            ["quiet", "listen", "checkout.updated"],
            ["call-1", "gap"],
            ["gap", "listen", "timeout"],
            ["gap", "listen", "checkout.updated"],
            ["listen", "quiet", "checkout.updated"],
        ],
        "goal": {"topics": ["order.placed"]},
    }


def _on_quiet(context: Optional[Dict[str, Any]] = None) -> EnrollmentRun:
    run = _run()
    run.current_node = "quiet"
    run.context = {"phone": "+919876543210", **(context or {})}
    return run


def test_a_timer_ending_after_hours_holds_the_run_until_the_window_opens(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """No call is queued at night: the run stays on quiet, still listening,
    with its alarm at the next opening."""
    fired: List[str] = []
    _quiet_actions(monkeypatch, fired)
    writes = _Writes(matched=True, definition=_windowed(_CLOSED))
    _install(monkeypatch, writes)
    run = _on_quiet()
    _advance(writes, run)
    assert fired == []
    assert writes.calls == [
        ("advance", (str(run.id), "quiet", _NEXT_OPENING, run.context, LEASE))
    ]


def test_inside_the_hours_the_timer_moves_on_and_the_next_wait_honours_them(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """17:30 is open: the call goes out now. gap's 30 minutes would end at
    18:00, when the window closes (`to` is exclusive), so its alarm is the
    next opening."""
    fired: List[str] = []
    _quiet_actions(monkeypatch, fired)
    writes = _Writes(matched=True, definition=_windowed(_OPEN))
    _install(monkeypatch, writes)
    _advance(writes, _on_quiet())
    assert fired == ["call"]
    ((verb, args),) = writes.calls
    assert (verb, args[1], args[2]) == ("advance", "gap", _NEXT_OPENING)


def test_a_letter_is_never_held_by_the_window(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """The hours are shut, but a letter woke the square: the run follows the
    letter's arrow now, so the rule is judged on it at night."""
    fired: List[str] = []
    _quiet_actions(monkeypatch, fired)
    writes = _Writes(matched=True, definition=_windowed(_CLOSED))
    _install(monkeypatch, writes)
    _advance(writes, _on_quiet({"reply_quiet": "checkout.updated"}))
    assert fired == []
    ((verb, args),) = writes.calls
    assert (verb, args[1], args[2]) == ("advance", "listen", NOW + timedelta(days=1))


# --- one wait (17 Sep 2026): old documents walk, minutes are optional ---------


def test_a_stored_wait_event_document_still_walks(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """The version rows are immutable and thousands say wait_event: a reply
    takes its labelled arrow and the timer takes timeout, as before."""
    old = {
        **_ASK_TWICE,
        "nodes": [
            {**_ASK_TWICE["nodes"][0], "type": "wait_event"},
            _ASK_TWICE["nodes"][1],
        ],
    }
    old["edges"] = [["ask", "wait-1d", "YES"], ["ask", "wait-1d", "timeout"]]
    for context in (
        {"phone": "+919876543210", "reply_ask": "YES"},
        {"phone": "+919876543210"},
    ):
        writes = _Writes(matched=True, definition=old)
        _install(monkeypatch, writes)
        run = _run()
        run.current_node = "ask"
        run.context = dict(context)
        _advance(writes, run)
        ((verb, args),) = writes.calls
        assert (verb, args[1], args[2]) == (
            "advance",
            "wait-1d",
            NOW + timedelta(days=1),
        )


def test_arriving_on_a_listening_wait_without_minutes_sleeps_for_the_runs_life(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    board = {
        **_TWO_WAITS,
        "nodes": [
            {"id": "wait-30m", "type": "wait", "minutes": 30},
            {"id": "wait-1d", "type": "wait", "topics": ["x"], "key": "$topic"},
        ],
        "edges": [["wait-30m", "wait-1d"]],
    }
    writes = _Writes(matched=True, definition=board)
    _install(monkeypatch, writes)
    run = _run()
    _advance(writes, run)
    ((verb, args),) = writes.calls
    life_ends = run.entered_at + timedelta(days=7)  # the default max_age_days
    assert (verb, args[1], args[2]) == (
        "advance",
        "wait-1d",
        life_ends + timedelta(minutes=1),
    )


def test_a_wait_until_the_window_that_is_already_open_moves_on_in_the_same_visit(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """No minutes and the hours are open (17:30 IST inside 09:00-18:00): the
    alarm is due, so the walker walks straight on to the next wait."""
    board = {
        **_TWO_WAITS,
        "nodes": [
            {"id": "wait-30m", "type": "wait", "minutes": 30},
            {"id": "till-open", "type": "wait", "window": _OPEN},
            {"id": "wait-1d", "type": "wait", "minutes": 1440},
        ],
        "edges": [["wait-30m", "till-open"], ["till-open", "wait-1d"]],
    }
    writes = _Writes(matched=True, definition=board)
    _install(monkeypatch, writes)
    _advance(writes, _run())
    ((verb, args),) = writes.calls
    assert (verb, args[1], args[2]) == ("advance", "wait-1d", NOW + timedelta(days=1))


def test_arriving_on_a_listening_wait_with_a_window_listens_even_when_the_hours_are_open(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """17:30 IST is inside 09:00-18:00, but the square lists topics and has no
    minutes: it listens for the run's life, it does not walk straight through
    and take its timeout arrow."""
    board = {
        **_TWO_WAITS,
        "nodes": [
            {"id": "wait-30m", "type": "wait", "minutes": 30},
            {
                "id": "listen",
                "type": "wait",
                "topics": ["x"],
                "key": "$topic",
                "window": _OPEN,
            },
            {"id": "wait-1d", "type": "wait", "minutes": 1440},
        ],
        "edges": [["wait-30m", "listen"], ["listen", "wait-1d", "timeout"]],
    }
    writes = _Writes(matched=True, definition=board)
    _install(monkeypatch, writes)
    run = _run()
    _advance(writes, run)
    ((verb, args),) = writes.calls
    life_ends = run.entered_at + timedelta(days=7, minutes=1)  # 17:00 IST, open
    assert (verb, args[1], args[2]) == ("advance", "listen", life_ends)

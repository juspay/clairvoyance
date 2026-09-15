"""What each line on the workflow path carries as FIELDS.

Alert rules read columns, so these names are a contract with the log
store: rename one and a rule goes quiet WITHOUT failing. That is what
these tests exist to stop. Nothing here reads the database — counting and
ratios are the log store's job.

The rest of the path is tested where its fakes already live:
test_event_worker.py, test_dispatch.py, test_crm_auth.py,
test_worker_runtime.py.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple, cast
from uuid import uuid4

import pytest

import app.crm.outreach.enrol as enrol_mod
import app.crm.outreach.entry as entry
import app.crm.outreach.walker as walker
import app.crm.record.ingest as ingest
from app.core.logger.context import clear_log_context, get_log_context
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
LEASE = NOW + timedelta(seconds=300)


@pytest.fixture(autouse=True)
def clean_context() -> Any:
    """The context is a ContextVar the whole process shares — a test that
    left one behind would prove the next one's assertion for it."""
    clear_log_context()
    yield
    clear_log_context()


class _Lines:
    """A logger that records what each line BOUND, not what it said.
    ``bind`` returns a fresh view sharing one recording (loguru's
    contract), so each line keeps its own fields."""

    def __init__(self, sink: Optional[List[Tuple[Dict[str, Any], str]]] = None) -> None:
        self.lines: List[Tuple[Dict[str, Any], str]] = [] if sink is None else sink
        self._bound: Dict[str, Any] = {}

    def bind(self, **fields: Any) -> "_Lines":
        view = _Lines(self.lines)
        view._bound = {**self._bound, **fields}
        return view

    def _record(self, message: Any) -> None:
        self.lines.append((dict(self._bound), str(message)))

    info = warning = error = _record

    def fields(self, needle: str) -> Dict[str, Any]:
        """The fields of the one line whose text contains ``needle``."""
        hits = [fields for fields, text in self.lines if needle in text]
        assert len(hits) == 1, f"expected one {needle!r} line, got {len(hits)}"
        return hits[0]


class _Claim:
    """The accessor slice the claim calls: hands back a fixed batch."""

    def __init__(self, runs: List[EnrollmentRun]) -> None:
        self.runs = runs
        self.leases: List[int] = []

    async def claim_due_runs(self, limit: int, lease: int) -> List[EnrollmentRun]:
        self.leases.append(lease)
        return self.runs[:limit]


def _run(
    node: str = "nudge-call",
    lease: Optional[datetime] = None,
    attempts: int = 1,
) -> EnrollmentRun:
    """Default: NO lease, so walk_run logs and returns at once — the
    context is stamped before that check, which is the point. Pass
    ``lease`` when the visit has to get as far as doing work."""
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=uuid4(),
        workflow_version=1,
        customer_id=uuid4(),
        status="waiting",
        current_node=node,
        wake_at=lease,
        entered_at=NOW - timedelta(minutes=5),
        exited_at=None,
        exit_reason=None,
        context={},
        enrollment_key="k1",
        attempts=attempts,
        last_error=None,
    )


def _capture(monkeypatch: pytest.MonkeyPatch) -> _Lines:
    lines = _Lines()
    monkeypatch.setattr(walker, "logger", lines)
    return lines


def test_a_walked_run_puts_its_ids_on_the_line_as_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(monkeypatch)
    run = _run()

    async def walk_then_read() -> Dict[str, Any]:
        # Read INSIDE the task: asyncio.run() would hand back a copy.
        # This is also what the loop does — it awaits the handler itself.
        await walker.walk_run(run)
        return get_log_context()

    context = asyncio.run(walk_then_read())
    assert context["component"] == walker.LOG_COMPONENT
    assert context["merchant_id"] == "m1"
    assert context["workflow_id"] == str(run.workflow_id)
    assert context["run_id"] == str(run.id)
    assert context["node"] == "nudge-call"


class _Spec:
    """A node type, reduced to what _advance asks of the registry."""

    def __init__(self, execute: Any = None, branches: bool = False) -> None:
        self.execute = execute
        self.branches = branches


def test_a_multi_square_visit_names_the_square_it_broke_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One claim crosses consecutive immediate squares. walk_run stamps
    the square the token ARRIVED on, so without a refresh the park below
    blames 'first' for what 'second' did — and `node` is the field that
    says WHAT broke, so a stale one is worse than none."""
    lines = _capture(monkeypatch)
    monkeypatch.setattr(walker, "enrollment_accessor", _Parks())
    monkeypatch.setattr(walker, "workflow_accessor", _Parks())

    async def fine(run: Any, node: Any, definition: Any) -> Dict[str, Any]:
        return {}

    async def broken(run: Any, node: Any, definition: Any) -> Dict[str, Any]:
        raise NodeParked(f"{node.id} asked for something impossible")

    definition = SimpleNamespace(
        nodes=[
            SimpleNamespace(id="first", type="ok", window=None),
            SimpleNamespace(id="second", type="bad", window=None),
        ],
        outgoing=lambda: {},
        exits=SimpleNamespace(max_age_days=30),
        goal_tiers=lambda: [],
    )

    async def pinned(run: Any) -> Any:
        return definition

    monkeypatch.setattr(walker, "definition_for", pinned)
    monkeypatch.setattr(walker, "NODE_TYPES", {"ok": _Spec(fine), "bad": _Spec(broken)})
    monkeypatch.setattr(walker, "is_wait", lambda node: False)
    monkeypatch.setattr(walker, "branches", lambda node: False)
    monkeypatch.setattr(
        walker,
        "pick_next",
        lambda node, edges, context: "second" if node.id == "first" else None,
    )

    async def walk_then_read() -> Dict[str, Any]:
        await walker.walk_run(_run(node="first", lease=LEASE))
        return get_log_context()

    context = asyncio.run(walk_then_read())
    assert context["node"] == "second"
    # update_, not set_: the ids walk_run stamped must survive the refresh,
    # or the park loses the run it belongs to.
    assert context["merchant_id"] == "m1" and context["run_id"]
    assert lines.fields("parked")["park_kind"] == "defect"


def test_the_claim_resets_the_context_it_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the reset, the next pass's lines — and the heartbeat
    between them — would be filed under whoever was walked last."""
    _capture(monkeypatch)
    monkeypatch.setattr(walker, "enrollment_accessor", _Claim([]))

    async def walk_then_claim() -> Tuple[Dict[str, Any], Dict[str, Any]]:
        await walker.walk_run(_run())
        stamped = get_log_context()
        await walker.claim_due_runs(50)
        return stamped, get_log_context()

    stamped, after_claim = asyncio.run(walk_then_claim())
    assert "run_id" in stamped  # the leak exists to be cleared
    assert after_claim == {"component": walker.LOG_COMPONENT}


def test_the_pass_line_reports_its_batch_as_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full batch left runs due behind it — the backlog signal, with
    no table read owed."""
    lines = _capture(monkeypatch)
    monkeypatch.setattr(
        walker, "enrollment_accessor", _Claim([_run() for _ in range(3)])
    )

    claimed = asyncio.run(walker.claim_due_runs(3))  # batch filled
    assert len(claimed) == 3
    fields = lines.fields("walker pass")
    assert fields["claimed"] == 3
    assert fields["batch_full"] is True

    lines.lines.clear()
    asyncio.run(walker.claim_due_runs(10))  # room to spare
    assert lines.fields("walker pass")["batch_full"] is False


def test_an_empty_claim_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The walker polls every few seconds; the heartbeat already proves
    liveness, so an idle pass that logged would only bury signal."""
    lines = _capture(monkeypatch)
    monkeypatch.setattr(walker, "enrollment_accessor", _Claim([]))

    assert asyncio.run(walker.claim_due_runs(50)) == []
    assert lines.lines == []


# --- the door: traffic, and how long the store took (8) ---


def test_every_stored_letter_reports_its_merchant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only place a merchant's TRAFFIC is visible: a sender that
    stops raises no error anywhere. No timing here — one DB call is not
    the request, and the load balancer times the whole thing."""
    lines = _Lines()
    monkeypatch.setattr(ingest, "logger", lines)

    async def stored(*args: Any, **kwargs: Any) -> str:
        return "evt-1"

    monkeypatch.setattr(ingest.accessor, "insert_event", stored)

    asyncio.run(
        ingest.ingest_event(
            merchant_id="flipkart",
            source="credit",
            topic="INITIATED",
            external_id="x-1",
            payload={"customer_id": "c-1"},
        )
    )

    fields = lines.fields("event accepted")
    assert fields["component"] == ingest.LOG_COMPONENT
    assert fields["merchant_id"] == "flipkart"
    assert fields["source"] == "credit" and fields["topic"] == "INITIATED"
    assert fields["duplicate"] is False
    # 14: where the trail starts. Without it the door is the one hop
    # "what happened to this order" cannot cross.
    assert fields["event_id"] == "evt-1"


def test_a_deduped_letter_is_counted_but_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Still traffic, but a producer stuck retrying must not read as
    healthy volume."""
    lines = _Lines()
    monkeypatch.setattr(ingest, "logger", lines)

    async def duplicate(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ingest.accessor, "insert_event", duplicate)

    asyncio.run(
        ingest.ingest_event(
            merchant_id="m1",
            source="s",
            topic="t",
            external_id="x-1",
            payload={},
        )
    )
    fields = lines.fields("event accepted")
    assert fields["duplicate"] is True
    # Honest rather than tidy: nothing was filed, so there is no id to
    # follow. A rule that joins on it skips this line instead of
    # matching the wrong letter.
    assert fields["event_id"] is None


def test_a_store_failure_logs_no_acceptance(monkeypatch: pytest.MonkeyPatch) -> None:
    """An accepted line here would count a letter never stored; the
    raise becomes the door's own 503 line."""
    lines = _Lines()
    monkeypatch.setattr(ingest, "logger", lines)

    async def broken(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("db gone")

    monkeypatch.setattr(ingest.accessor, "insert_event", broken)

    with pytest.raises(RuntimeError):
        asyncio.run(
            ingest.ingest_event(
                merchant_id="m1", source="s", topic="t", external_id="x", payload={}
            )
        )
    assert lines.lines == []


# --- entry: the denominator, and the refusals (9) ---


def _plan(status: str = "live") -> Any:
    return SimpleNamespace(id=uuid4(), status=status, definition={"nodes": []})


def test_a_refused_enrolment_names_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusals are mostly the plan working as written; what they feed
    is the comparison, grouped by which refusal grew."""
    lines = _Lines()
    monkeypatch.setattr(enrol_mod, "logger", lines)

    run = asyncio.run(
        enrol_mod.enrol(
            merchant_id="m1",
            workflow=_plan(status="paused"),
            customer_id="c-1",
            context={},
        )
    )

    assert run is None
    fields = lines.fields("enrol skipped")
    assert fields["skip_reason"] == "not_live"
    assert fields["merchant_id"] == "m1"
    assert fields["component"] == enrol_mod.LOG_COMPONENT
    # Each identifier under its OWN name, and only where it applies: one
    # field holding a status here and a customer on the next path would be
    # nothing a query could group by.
    assert fields["workflow_status"] == "paused"
    assert "customer_id" not in fields


def test_the_refusal_that_reads_as_silence_shares_the_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The break nobody sees: every event arrives, every one is refused,
    no run starts. It counts beside the expected refusals."""
    lines = _Lines()
    monkeypatch.setattr(entry, "logger", lines)
    door = SimpleNamespace(key="customer_id")
    event = SimpleNamespace(
        id="evt-1", merchant_id="m1", payload={}, source="credit", topic="INITIATED"
    )

    admit, key = entry._enrollment_key(cast(Any, door), cast(Any, event), "wf-1")

    assert (admit, key) == (False, None)
    fields = lines.fields("enrol skipped")
    assert fields["skip_reason"] == "entry_key_missing"
    assert fields["merchant_id"] == "m1" and fields["workflow_id"] == "wf-1"


# --- 14: the letter's id, carried across the door ---


class _Admits:
    """The accessor slice one successful enrolment touches."""

    async def source_event_used(self, txn: Any, *args: Any) -> bool:
        return False

    async def admission_facts(
        self, txn: Any, *args: Any, **kwargs: Any
    ) -> Dict[str, Any]:
        return {"runs": 0, "latest_entered_at": None}

    async def lock_templates_shared(self, txn: Any, *args: Any) -> None:
        return None

    async def insert_enrollment(
        self,
        txn: Any,
        merchant_id: str,
        workflow_id: str,
        workflow_version: int,
        customer_id: str,
        current_node: str,
        wake_at: datetime,
        context: Dict[str, Any],
        enrollment_key: str,
    ) -> Any:
        return SimpleNamespace(id=uuid4(), current_node=current_node)


_DEFINITION = {
    "entry": {"topic": "checkout.initiated"},
    "nodes": [{"id": "wait-30m", "type": "wait", "minutes": 30}],
    "edges": [],
    "goal": {"topics": ["order.placed"]},
}


def _enrol(
    monkeypatch: pytest.MonkeyPatch,
    context: Dict[str, Any],
    commit_fails: bool = False,
) -> _Lines:
    """Run enrol() through the whole door, with atomically replaced by a
    passthrough — or by one whose COMMIT fails after the body ran."""
    lines = _Lines()
    monkeypatch.setattr(enrol_mod, "logger", lines)
    monkeypatch.setattr(enrol_mod, "enrollment_accessor", _Admits())
    monkeypatch.setattr(enrol_mod, "version_accessor", _Admits())

    async def atomically(fn: Any, *args: Any) -> Any:
        result = await fn(cast(Any, object()), *args)
        if commit_fails:
            raise RuntimeError("commit failed")
        return result

    monkeypatch.setattr(enrol_mod, "atomically", atomically)
    workflow = SimpleNamespace(
        id=uuid4(), version=1, status="live", definition=_DEFINITION
    )
    coro = enrol_mod.enrol(
        merchant_id="m1",
        workflow=cast(Any, workflow),
        customer_id=str(uuid4()),
        context=context,
    )
    if commit_fails:
        with pytest.raises(RuntimeError):
            asyncio.run(coro)
    else:
        asyncio.run(coro)
    return lines


def _enrolled_line(monkeypatch: pytest.MonkeyPatch, context: Dict[str, Any]) -> Any:
    return _enrol(monkeypatch, context).fields("enrolled")


def test_a_run_names_the_letter_that_started_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other end of the trail. The ingest line files under event_id;
    this one files the same value under source_event_id, so the two hops
    join instead of being matched by eye on timestamps."""
    fields = _enrolled_line(monkeypatch, {"source_event_id": "evt-1"})
    assert fields["source_event_id"] == "evt-1"
    assert fields["run_id"] and fields["component"] == enrol_mod.LOG_COMPONENT


def test_a_replayed_letter_is_a_named_refusal_not_a_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The at-least-once scan re-reads events, and the second pass makes
    no run. Unlogged, this is the one way "events arriving, no runs
    starting" can be both normal and unaccounted for — so it counts
    beside the other refusals under its own word."""
    lines = _Lines()
    monkeypatch.setattr(enrol_mod, "logger", lines)

    class _Replayed(_Admits):
        async def source_event_used(self, txn: Any, *args: Any) -> bool:
            return True

    monkeypatch.setattr(enrol_mod, "enrollment_accessor", _Replayed())
    monkeypatch.setattr(enrol_mod, "version_accessor", _Replayed())
    workflow = SimpleNamespace(id=uuid4(), version=1, status="live")
    definition = WorkflowDefinition.model_validate(
        {
            "entry": {"topic": "checkout.initiated"},
            "nodes": [{"id": "wait-30m", "type": "wait", "minutes": 30}],
            "edges": [],
            "goal": {"topics": ["order.placed"]},
        }
    )

    run = asyncio.run(
        enrol_mod._enrol_in_txn(
            cast(Any, object()),
            "m1",
            cast(Any, workflow),
            definition,
            definition.entries[0],
            str(uuid4()),
            {"source_event_id": "evt-1"},
            "k1",
        )
    )

    assert run is None
    assert lines.fields("enrol skipped")["skip_reason"] == "source_event_replayed"


def test_a_run_with_no_triggering_letter_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not every run comes from an event (a broadcast will not). The
    field is present and empty rather than absent — a column that
    appears on some lines and not others is one a rule cannot filter on."""
    assert _enrolled_line(monkeypatch, {})["source_event_id"] is None


def test_a_failed_commit_counts_no_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The enrolled line is THE denominator, so it must be emitted after
    the atom commits — logged inside the body, a commit failure would
    count a run that was rolled back and never existed."""
    lines = _enrol(monkeypatch, {}, commit_fails=True)
    assert not any("enrolled" in text for _, text in lines.lines)


# --- the walker: why a run parked, and how it ended (10, 11) ---


class _Parks:
    """The accessor slice a parking visit touches."""

    def __init__(self) -> None:
        self.parked: List[Tuple[str, str]] = []

    async def get_workflow(self, merchant_id: str, workflow_id: str) -> Any:
        return SimpleNamespace(status="live")

    async def park_run(self, run_id: str, last_error: str, lease: Any) -> bool:
        self.parked.append((run_id, last_error))
        return True


class _Retries(_Parks):
    """A visit that fails with attempts left: the run goes back on the
    clock instead of parking."""

    async def record_run_error(
        self, run_id: str, last_error: str, retry_in: int, lease: Any
    ) -> bool:
        return True


def _park(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, attempts: int = 1
) -> Dict[str, Any]:
    lines = _Lines()
    monkeypatch.setattr(walker, "logger", lines)
    monkeypatch.setattr(walker, "enrollment_accessor", _Parks())
    monkeypatch.setattr(walker, "workflow_accessor", _Parks())

    async def fails(run: Any) -> Any:
        raise failure

    monkeypatch.setattr(walker, "definition_for", fails)
    run = _run(lease=LEASE, attempts=attempts)
    asyncio.run(walker.walk_run(run))
    return lines.fields("parked")


def test_a_defect_and_an_exhausted_run_park_under_different_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same dead end, different fix — so a field, not a message prefix."""
    defect = _park(monkeypatch, NodeParked("template t-1 is gone"))
    assert defect["park_kind"] == "defect"

    worn_out = _park(
        monkeypatch, RuntimeError("db flaked"), attempts=walker.CRM_WALKER_MAX_ATTEMPTS
    )
    assert worn_out["park_kind"] == "attempts_exhausted"


def test_a_park_never_calls_its_kind_a_reason_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reason_class is dispatch's word, with a fixed vocabulary
    (provider · merchant · policy). A park reusing the name would put
    'defect' in the same column and silently spoil every rule that
    groups by it — the failure mode is a wrong count, not an error."""
    for failure, attempts in (
        (NodeParked("gone"), 1),
        (RuntimeError("db flaked"), walker.CRM_WALKER_MAX_ATTEMPTS),
    ):
        assert "reason_class" not in _park(monkeypatch, failure, attempts=attempts)


# --- 13: is it settled, or still on the ladder ---


def test_a_park_is_settled_and_a_retry_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate on raising anything: a failure still inside its retry
    ladder is not yet anybody's problem. Both parks are the last word;
    a run going back on the clock is not."""
    assert _park(monkeypatch, NodeParked("gone"))["permanent"] is True
    assert (
        _park(
            monkeypatch,
            RuntimeError("db flaked"),
            attempts=walker.CRM_WALKER_MAX_ATTEMPTS,
        )["permanent"]
        is True
    )

    lines = _Lines()
    monkeypatch.setattr(walker, "logger", lines)
    monkeypatch.setattr(walker, "enrollment_accessor", _Retries())
    monkeypatch.setattr(walker, "workflow_accessor", _Parks())

    async def fails(run: Any) -> Any:
        raise RuntimeError("db flaked")

    monkeypatch.setattr(walker, "definition_for", fails)
    asyncio.run(walker.walk_run(_run(lease=LEASE)))

    retry = lines.fields("retries in")
    assert retry["permanent"] is False
    assert retry["attempts"] == 1 and retry["retry_in_s"] > 0


def test_every_exit_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise written only to the row, where no rule can see it."""
    lines = _Lines()
    monkeypatch.setattr(walker, "logger", lines)
    run = _run(lease=LEASE)

    for reason in ("timed_out", "goal_met", "completed", "ejected"):
        lines.lines.clear()
        walker._log_exit(run, reason)
        assert lines.fields("exited")["exit_reason"] == reason

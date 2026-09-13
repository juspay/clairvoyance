"""Conditions on a goal tier (rollout phase 20).

A goal tier said WHICH topics end a run and, since phase 06, which RUN a
letter is about. It could not say what the letter had to CONTAIN — fine
while every goal topic was a once-per-order fact, wrong the moment the
topic is a many-reasons one. Shopify fires orders/updated for a
fulfilment, a note, an address fix and a tag write alike, so "the topic
arrived" is not the goal.

`goal.where` is the door's own typed grammar on the verdict side, and the
tests below pin the three things that make it safe rather than merely
present: the live consumer declines the wrong edit; the WALKER's
re-check declines it too (the bypass that would otherwise undo the filter
at the next claim); and the publish validator refuses a condition the
catalog cannot answer, instead of letting it publish and never fire.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import pytest

import app.crm.outreach.definitions as definitions
import app.crm.outreach.entry as entry
import app.crm.outreach.walker as walker
from app.crm.outreach.catalog_laws import goals_against_catalog
from app.crm.outreach.entry import consume_attributed_event
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import (
    EnrollmentRun,
    Workflow,
    WorkflowDefinition,
)
from app.crm.record.catalog import code_entries
from app.crm.record.db.queries import customer_goal_events_query
from app.crm.record.schemas import RawEvent

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)


class _Frozen(datetime):
    """`datetime` with `now()` pinned to NOW — a subclass, so the walker's
    arithmetic against entered_at still behaves."""

    @classmethod
    def now(cls, tz: Optional[timezone] = None) -> datetime:  # type: ignore[override]
        return NOW


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixtures are dated; walk_run asks the REAL clock for
    `now - entered_at > max_age_days` (default 7), so they expire on a date."""
    monkeypatch.setattr(walker, "datetime", _Frozen)


LEASE = NOW + timedelta(seconds=300)

CONFIRMED = "Buddy Confirmed"

# The board the WhatsApp confirmation flow actually publishes: admitted on
# a COD order, tagged by an action square, and ended by Shopify's echo of
# THAT tag — never by the fulfilment that follows it.
_TAG_GOAL: Dict[str, Any] = {
    "topics": ["orders/updated"],
    "key": {"event": "id", "run": "id"},
    "where": [{"field": "payload.tags", "op": "has_all", "value": [CONFIRMED]}],
    "exit_reason": "goal_met",
}
_PLAN: Dict[str, Any] = {
    "entry": {"topic": "orders/create", "key": "id"},
    "nodes": [{"id": "hold", "type": "wait", "minutes": 30}],
    "edges": [],
    "goals": [_TAG_GOAL],
}


def _catalogs() -> Dict[str, Optional[Dict[str, Any]]]:
    """The real code-layer field maps, keyed the way gather_catalogs keys
    them — so these laws are checked against the catalog we actually ship,
    not a hand-written stand-in that could drift from it."""
    by_topic: Dict[str, Optional[Dict[str, Any]]] = {}
    for entry_ in code_entries():
        if entry_.source == "shopify":
            by_topic[entry_.topic] = {f.path: f for f in entry_.fields}
    return by_topic


# --- the vocabulary ---------------------------------------------------------


def test_a_goal_tier_carries_conditions_and_an_empty_where_is_the_old_behaviour() -> (
    None
):
    definition = WorkflowDefinition.model_validate(_PLAN)
    (tier,) = definition.goals
    assert [c.op for c in tier.where] == ["has_all"]
    plain = WorkflowDefinition.model_validate(
        {**_PLAN, "goals": [{"topics": ["orders/paid"]}]}
    )
    assert plain.goals[0].where == []


def test_tiers_are_judged_keyed_first() -> None:
    """The phase-06 rule, unchanged: the keyed tier says "THIS order" and a
    run it ends is no longer open for the unkeyed sweep. A `where` narrows a
    tier but does not reorder it — tiers are filtered by TOPIC first, so two
    tiers of one plan rarely compete at all."""
    unkeyed = {"topics": ["orders/updated"], "exit_reason": "converted_elsewhere"}
    definition = WorkflowDefinition.model_validate(
        {**_PLAN, "goals": [unkeyed, _TAG_GOAL]}  # least specific first
    )
    assert [t.exit_reason for t in definition.goal_tiers()] == [
        "goal_met",  # keyed
        "converted_elsewhere",  # unkeyed
    ]


def test_a_goal_key_must_be_a_top_level_payload_field() -> None:
    """The walker compares this key in SQL as payload->>$5 — one key,
    nothing deeper. Anything with a dot would work at the live consumer and
    silently match nothing at the next claim, so it is refused instead."""
    for bad in ("payload.id", "customer.id"):
        problems = goals_against_catalog(
            WorkflowDefinition.model_validate(
                {**_PLAN, "goals": [{**_TAG_GOAL, "key": {"event": bad, "run": "id"}}]}
            ),
            _catalogs(),
        )
        assert any("top-level payload field" in p for p in problems), (bad, problems)
    # the bare key is what the letter carries, and what the SQL can compare
    assert (
        goals_against_catalog(WorkflowDefinition.model_validate(_PLAN), _catalogs())
        == []
    )


# --- the publish laws -------------------------------------------------------


def test_a_condition_on_an_undeclared_goal_topic_is_refused() -> None:
    """Without this the plan publishes cleanly and silently never fires —
    the failure the door-side catalog law exists to prevent, one surface
    over."""
    problems = goals_against_catalog(
        WorkflowDefinition.model_validate(
            {**_PLAN, "goals": [{**_TAG_GOAL, "topics": ["orders/nobody-declares"]}]}
        ),
        _catalogs(),
    )
    assert any("not in the catalog" in p for p in problems), problems


def test_a_tier_without_conditions_may_still_name_any_topic() -> None:
    """Only a tier that asks a question about a payload has to prove the
    payload is one we can read."""
    assert (
        goals_against_catalog(
            WorkflowDefinition.model_validate(
                {**_PLAN, "goals": [{"topics": ["orders/nobody-declares"]}]}
            ),
            _catalogs(),
        )
        == []
    )


def test_a_list_op_on_a_text_field_is_refused() -> None:
    problems = goals_against_catalog(
        WorkflowDefinition.model_validate(
            {
                **_PLAN,
                "goals": [
                    {
                        **_TAG_GOAL,
                        "where": [
                            {"field": "payload.name", "op": "has_all", "value": ["x"]}
                        ],
                    }
                ],
            }
        ),
        _catalogs(),
    )
    assert any("is not an op" in p for p in problems), problems


def test_the_shipped_confirmation_plan_passes_every_law() -> None:
    assert validate_definition(_PLAN, catalogs=_catalogs()) == []


def test_a_nested_goal_key_is_refused_because_the_walker_compares_it_in_sql() -> None:
    """A nested path would work at the live consumer and silently match
    nothing at the walker — the two judging sites must agree."""
    problems = goals_against_catalog(
        WorkflowDefinition.model_validate(
            {
                **_PLAN,
                "goals": [{**_TAG_GOAL, "key": {"event": "customer.id", "run": "id"}}],
            }
        ),
        _catalogs(),
    )
    assert any("top-level payload field" in p for p in problems), problems


# --- the live consumer ------------------------------------------------------


def _flow() -> Workflow:
    return Workflow(
        id=uuid4(),
        merchant_id="m1",
        name="cod-confirmation",
        status="live",
        version=1,
        created_by=None,
        created_at=NOW,
        updated_at=NOW,
        definition=_PLAN,
        draft=None,
    )


def _run(flow: Workflow, order_id: str = "5201000001") -> EnrollmentRun:
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=flow.id,
        workflow_version=1,
        customer_id=uuid4(),
        status="waiting",
        current_node="hold",
        wake_at=NOW + timedelta(minutes=30),
        entered_at=NOW - timedelta(hours=1),
        exited_at=None,
        exit_reason=None,
        context={
            "id": order_id,
            "entered_event_at": (NOW - timedelta(hours=1)).isoformat(),
        },
        enrollment_key=order_id,
        attempts=0,
        last_error=None,
    )


def _event(payload: Dict[str, Any]) -> RawEvent:
    return RawEvent(
        id="ev-1",
        merchant_id="m1",
        source="shopify",
        topic="orders/updated",
        schema_version="1",
        external_id="orders/updated:1",
        payload=payload,
        received_at=NOW,
        occurred_at=NOW,
    )


class _Spine:
    def __init__(self, flow: Workflow, run: EnrollmentRun) -> None:
        self.flows = [flow]
        self.runs = [run]
        self.versions = {(str(flow.id), 1): _PLAN}
        self.cancels: List[Tuple[str, str]] = []

    async def live_workflows(self, merchant_id: str) -> List[Workflow]:
        return list(self.flows)

    async def open_runs_for_customer(self, *_: Any) -> List[EnrollmentRun]:
        return list(self.runs)

    async def get_definition(
        self, merchant_id: str, workflow_id: str, version: int
    ) -> Optional[Dict[str, Any]]:
        return self.versions.get((workflow_id, version))

    async def cancel_run(
        self,
        merchant_id: str,
        run_id: str,
        exit_reason: str,
        occurred_at: Optional[datetime] = None,
        key: Optional[Tuple[str, str]] = None,
        context_patch: Optional[Dict[str, Any]] = None,
    ) -> bool:
        self.cancels.append((run_id, exit_reason))
        return True

    async def resume_run_by_id(self, *_: Any, **__: Any) -> bool:
        return True

    async def patch_open_run(self, *_: Any, **__: Any) -> bool:
        return False


@pytest.fixture
def spine(monkeypatch: pytest.MonkeyPatch) -> _Spine:
    flow = _flow()
    fake = _Spine(flow, _run(flow))
    definitions._definitions.clear()
    for module, name in (
        (entry.workflow_accessor, "live_workflows"),
        (entry.enrollment_accessor, "open_runs_for_customer"),
        (definitions.version_accessor, "get_definition"),
        (entry.enrollment_accessor, "cancel_run"),
        (entry.enrollment_accessor, "resume_run_by_id"),
    ):
        monkeypatch.setattr(module, name, getattr(fake, name))
    return fake


def test_the_tag_edit_ends_the_run(spine: _Spine) -> None:
    asyncio.run(
        consume_attributed_event(
            _event({"id": 5201000001, "tags": f"{CONFIRMED}, COD"}), "c-1", {}
        )
    )
    assert [reason for _, reason in spine.cancels] == ["goal_met"]


def test_a_shipping_edit_on_the_same_order_ends_nothing(spine: _Spine) -> None:
    """The right topic about the right run — and the wrong edit. This is
    the whole point of the feature."""
    asyncio.run(
        consume_attributed_event(
            _event(
                {"id": 5201000001, "tags": "COD", "fulfillment_status": "fulfilled"}
            ),
            "c-1",
            {},
        )
    )
    assert spine.cancels == []


def test_the_tag_on_another_order_ends_nothing(spine: _Spine) -> None:
    asyncio.run(
        consume_attributed_event(
            _event({"id": 9999999999, "tags": f"{CONFIRMED}"}), "c-1", {}
        )
    )
    assert spine.cancels == []


# --- the walker's re-check (the bypass this phase closes) -------------------


class _Writes:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Any]] = []

    async def get_workflow(self, merchant_id: str, workflow_id: str) -> Workflow:
        return _flow()

    async def get_definition(self, *_: Any) -> Dict[str, Any]:
        return _PLAN

    async def exit_run(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append(("exit", args))
        return True

    async def advance_run(self, *args: Any) -> bool:
        self.calls.append(("advance", args))
        return True

    async def park_run(self, *args: Any) -> bool:
        self.calls.append(("park", args))
        return True

    async def record_run_error(self, *args: Any) -> bool:
        self.calls.append(("retry", args))
        return True


def _walk(monkeypatch: pytest.MonkeyPatch, letters: List[Dict[str, Any]]) -> _Writes:
    """One claim of a run standing on its wait, with `letters` as what the
    goal read returns."""
    writes = _Writes()
    from tests.crm.doubles import patch_accessors

    definitions._definitions.clear()
    patch_accessors(monkeypatch, walker, writes)
    patch_accessors(monkeypatch, definitions, writes)

    async def _events(*_: Any, **__: Any) -> List[RawEvent]:
        return [_event(payload) for payload in letters]

    async def _exists(*_: Any, **__: Any) -> bool:
        raise AssertionError(
            "a tier carrying a `where` must not be answered by the EXISTS — "
            "it knows only the topic and the key"
        )

    monkeypatch.setattr(walker, "customer_goal_events", _events)
    monkeypatch.setattr(walker, "customer_has_event", _exists)
    flow = _flow()
    run = _run(flow)
    asyncio.run(walker._advance(run, WorkflowDefinition.model_validate(_PLAN), LEASE))
    return writes


def test_the_walker_does_not_end_a_run_on_an_edit_the_where_declines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression. The SQL narrows to topic + order id, so a fulfilment
    on this order comes back from the read; without the conditions being
    re-applied here, the run would exit `goal_met` at its next claim and
    silently undo what the live consumer got right."""
    writes = _walk(
        monkeypatch, [{"id": 5201000001, "tags": "COD", "fulfillment_status": "x"}]
    )
    reasons = [args[1] for kind, args in writes.calls if kind == "exit"]
    assert "goal_met" not in reasons, writes.calls
    # It exits `completed` instead: this board is one square with no arrow
    # out, so the token walks off the end in the same visit. That is the
    # walker's own verdict about the BOARD, never a tier's about the goal —
    # and it is why a plan that wants Shopify's echo to be its record has
    # to keep a square to stand on while the echo travels.
    assert reasons == ["completed"]


def test_the_walker_ends_a_run_on_an_edit_the_where_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _walk(monkeypatch, [{"id": 5201000001, "tags": f"{CONFIRMED}, COD"}])
    exits = [args for kind, args in writes.calls if kind == "exit"]
    assert exits and exits[0][1] == "goal_met", writes.calls


def test_a_tier_without_conditions_still_uses_the_indexed_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every plan published before this phase costs exactly what it did:
    no payload read, one EXISTS."""
    writes = _Writes()
    from tests.crm.doubles import patch_accessors

    definitions._definitions.clear()
    patch_accessors(monkeypatch, walker, writes)
    patch_accessors(monkeypatch, definitions, writes)
    asked: List[str] = []

    async def _exists(*_: Any, **__: Any) -> bool:
        asked.append("exists")
        return False

    async def _events(*_: Any, **__: Any) -> List[RawEvent]:
        raise AssertionError("a tier with no `where` must not read payloads")

    monkeypatch.setattr(walker, "customer_has_event", _exists)
    monkeypatch.setattr(walker, "customer_goal_events", _events)
    plain = {**_PLAN, "goals": [{"topics": ["orders/paid"]}]}
    flow = _flow()
    asyncio.run(
        walker._advance(_run(flow), WorkflowDefinition.model_validate(plain), LEASE)
    )
    assert asked == ["exists"]


# --- the read behind it -----------------------------------------------------


def test_the_goal_events_query_is_bounded_newest_first_and_parameterised() -> None:
    query, params = customer_goal_events_query(
        "m1", "c-1", ["orders/updated"], NOW, ("id", "5201000001"), 50
    )
    assert "ORDER BY COALESCE(occurred_at, received_at) DESC" in query
    assert "LIMIT $7" in query
    assert "payload->>$5 = $6" in query
    assert params[-1] == 50
    # No value is ever spelled into the SQL text (one DB role, total blast
    # radius) — every one of them is a placeholder.
    for value in ("m1", "c-1", "5201000001"):
        assert value not in query


# --- a call's own facts reach the square after it (crm_mirror + telephony
#     spec + listened_facts) ----------------------------------------------


def test_gather_reads_both_goal_spellings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`goal` (singular) is the pre-phase-06 spelling. The MODEL rewrites it
    to `goals`, but gather_catalogs runs on the RAW document — before any
    model has touched it — so it must read both, or a legacy plan's goal
    topic is never gathered and its conditions are checked against nothing.

    Version rows are immutable, so those documents never go away."""
    import app.crm.outreach.catalog_laws as laws

    asked: List[str] = []

    async def _fields(_merchant: str, topic: str) -> Optional[Dict[str, Any]]:
        asked.append(topic)
        return {}

    monkeypatch.setattr(laws, "catalog_fields", _fields)
    base: Dict[str, Any] = {
        "entry": {"topic": "orders/create"},
        "nodes": [{"id": "w", "type": "wait", "minutes": 1}],
    }
    tier = {"topics": ["orders/updated"]}

    for spelling in ({"goals": [tier]}, {"goal": tier}):
        asked.clear()
        asyncio.run(laws.gather_catalogs("m1", {**base, **spelling}))
        assert "orders/updated" in asked, spelling

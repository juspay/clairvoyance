"""Conditions on a goal tier (rollout phase 21).

A goal tier said WHICH topics end a run and, since phase 06, which RUN a
letter is about. It could not say what the letter had to CONTAIN — fine
while every goal topic was a once-per-order fact, wrong the moment the
topic is a many-reasons one. Shopify fires orders/updated for a
fulfilment, a note, an address fix and a tag write alike, so "the topic
arrived" is not the goal.

`goal.where` is the door's own typed grammar on the verdict side, and the
tests below pin the three things that make it safe rather than merely
present: the live consumer declines the wrong edit; the WALKER's re-check
at fire time judges the order's newest letter the same way (so a letter
consumed before the run existed still ends it, and a shipping edit never
does); and the publish validator refuses a condition the catalog cannot
answer, instead of letting it publish and never fire.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import pytest

import app.crm.outreach.catalog_laws as laws
import app.crm.outreach.definitions as definitions
import app.crm.outreach.entry as entry
import app.crm.outreach.walker as walker
from app.crm.outreach.catalog_laws import goals_against_catalog
from app.crm.outreach.conditions import conditions_match
from app.crm.outreach.entry import consume_attributed_event
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import (
    EnrollmentRun,
    Workflow,
    WorkflowDefinition,
)
from app.crm.record.catalog import code_entries
from app.crm.record.schemas import RawEvent
from app.crm.shared.predicate import Condition
from tests.crm.doubles import patch_accessors

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
    "where": [{"field": "order_tags", "op": "includes", "value": CONFIRMED}],
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
    assert [c.op for c in tier.where] == ["includes"]
    plain = WorkflowDefinition.model_validate(
        {**_PLAN, "goals": [{"topics": ["orders/cancelled"]}]}
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
                            {"field": "payload.name", "op": "includes", "value": "x"}
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


def test_the_key_laws_hold_without_a_catalog() -> None:
    """They need no field map, so a pure caller (catalogs=None) meets them
    too."""
    dotted = {
        **_PLAN,
        "goals": [{**_TAG_GOAL, "key": {"event": "payload.id", "run": "id"}}],
    }
    assert any(
        "top-level payload field" in p
        for p in validate_definition(dotted, catalogs=None)
    )


def test_a_tier_with_conditions_needs_a_key() -> None:
    """The walker judges the NEWEST letter of the tier's topics; unkeyed, that
    letter may be about another of her orders."""
    unkeyed = {k: v for k, v in _TAG_GOAL.items() if k != "key"}
    problems = validate_definition({**_PLAN, "goals": [unkeyed]}, catalogs=_catalogs())
    assert any("needs a `key`" in p for p in problems), problems


@pytest.mark.parametrize("value", ["", "   ", 1])
def test_a_tag_value_that_can_never_match_is_refused(value: Any) -> None:
    """order_tags holds trimmed, non-blank text, and 1 never equals "1" — a
    goal that never fires keeps calling."""
    tier = {
        **_TAG_GOAL,
        "where": [{"field": "order_tags", "op": "includes", "value": value}],
    }
    problems = validate_definition({**_PLAN, "goals": [tier]}, catalogs=_catalogs())
    assert any("non-blank text value" in p for p in problems), problems


def test_publish_refuses_a_goal_condition_the_catalog_cannot_answer() -> None:
    """Through the publish validator, not goals_against_catalog directly: the
    one line that wires the goal laws into publish must not go quietly."""
    bad = {
        **_PLAN,
        "goals": [
            {
                **_TAG_GOAL,
                "where": [{"field": "payload.name", "op": "includes", "value": "x"}],
            }
        ],
    }
    assert any(
        "is not an op" in p for p in validate_definition(bad, catalogs=_catalogs())
    )


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
        steps: Optional[List[Dict[str, Any]]] = None,
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


# --- the walker's re-check at fire time -------------------------------------


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


def _claim(
    monkeypatch: pytest.MonkeyPatch, newest: Optional[RawEvent]
) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    """One walker claim of the run on `hold`, with the order's newest letter
    stubbed. Returns the exit reasons and what the newest-letter read was
    asked."""
    writes = _Writes()
    definitions._definitions.clear()
    patch_accessors(monkeypatch, walker, writes)
    patch_accessors(monkeypatch, definitions, writes)
    asked: List[Tuple[Any, ...]] = []

    async def _latest(*args: Any) -> Optional[RawEvent]:
        asked.append(args)
        return newest

    async def _exists(*_: Any, **__: Any) -> bool:
        raise AssertionError("a tier carrying a `where` must not ask the EXISTS")

    monkeypatch.setattr(walker, "latest_customer_event", _latest)
    monkeypatch.setattr(walker, "customer_has_event", _exists)
    asyncio.run(
        walker._advance(_run(_flow()), WorkflowDefinition.model_validate(_PLAN), LEASE)
    )
    return [args[1] for kind, args in writes.calls if kind == "exit"], asked


def test_a_letter_consumed_before_the_run_existed_still_ends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression (canon/06-outreach.md: goals are re-checked at fire
    time). The tag letter was consumed while no run existed — a create
    consumed late, or Shopify delivering out of order — so the live consumer
    ended nothing. The first claim finds it and exits `goal_met` instead of
    walking on."""
    reasons, asked = _claim(
        monkeypatch, _event({"id": 5201000001, "tags": f"{CONFIRMED}, COD"})
    )
    assert reasons == ["goal_met"]
    # ONE row, about THIS order: the tier's topics and its key.
    (args,) = asked
    assert args[2] == ["orders/updated"] and args[4] == ("id", "5201000001")


def test_a_newest_letter_that_no_longer_matches_ends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shipping edit on the same order is the newest letter: the EXISTS
    would have said yes, the conditions say no. `completed`, not `goal_met`:
    this board is one square with no arrow out, so the token walks off the
    end — the walker's verdict about the BOARD, never a tier's."""
    reasons, _ = _claim(
        monkeypatch,
        _event({"id": 5201000001, "tags": "COD", "fulfillment_status": "fulfilled"}),
    )
    assert reasons == ["completed"]


def test_no_letter_about_the_order_ends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasons, _ = _claim(monkeypatch, None)
    assert reasons == ["completed"]


def test_a_tier_without_conditions_still_uses_the_indexed_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every plan published before this phase costs exactly what it did:
    one EXISTS, and no payload read."""
    writes = _Writes()
    definitions._definitions.clear()
    patch_accessors(monkeypatch, walker, writes)
    patch_accessors(monkeypatch, definitions, writes)
    asked: List[str] = []

    async def _exists(*_: Any, **__: Any) -> bool:
        asked.append("exists")
        return False

    async def _latest(*_: Any) -> None:
        raise AssertionError("a tier without `where` must not read a payload")

    monkeypatch.setattr(walker, "customer_has_event", _exists)
    monkeypatch.setattr(walker, "latest_customer_event", _latest)
    plain = {**_PLAN, "goals": [{"topics": ["orders/cancelled"]}]}
    asyncio.run(
        walker._advance(_run(_flow()), WorkflowDefinition.model_validate(plain), LEASE)
    )
    assert asked == ["exists"]


# --- the gather ---------------------------------------------------------------


def test_gather_reads_both_goal_spellings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`goal` (singular) is the pre-phase-06 spelling. The MODEL rewrites it
    to `goals`, but gather_catalogs runs on the RAW document — before any
    model has touched it — so it must read both, or a legacy plan's goal
    topic is never gathered and its conditions are checked against nothing.

    Version rows are immutable, so those documents never go away."""
    asked: List[str] = []

    async def _fields(_merchant: str, topic: str) -> Optional[Dict[str, Any]]:
        asked.append(topic)
        return {}

    monkeypatch.setattr(laws, "catalog_fields", _fields)
    base: Dict[str, Any] = {
        "entry": {"topic": "orders/create"},
        "nodes": [{"id": "w", "type": "wait", "minutes": 1}],
    }
    tier = {"topics": ["orders/updated"], "where": _TAG_GOAL["where"]}

    for spelling in ({"goals": [tier]}, {"goal": tier}):
        asked.clear()
        asyncio.run(laws.gather_catalogs("m1", {**base, **spelling}))
        assert "orders/updated" in asked, spelling


def test_gather_skips_a_tier_without_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing reads a plain tier's field map, so it is not gathered — a
    topic two sources declare would otherwise refuse a plan release accepts
    (AmbiguousTopic)."""
    asked: List[str] = []

    async def _fields(_merchant: str, topic: str) -> Optional[Dict[str, Any]]:
        asked.append(topic)
        return {}

    monkeypatch.setattr(laws, "catalog_fields", _fields)
    asyncio.run(
        laws.gather_catalogs(
            "m1",
            {
                "entry": {"topic": "checkouts/create"},
                "nodes": [{"id": "w", "type": "wait", "minutes": 1}],
                "goals": [{"topics": ["orders/create"]}],
            },
        )
    )
    assert asked == ["checkouts/create"]


# --- one evaluator for doors, goals and the walker's re-check --------------


def test_a_goal_reads_an_array_path_as_the_door_does() -> None:
    """A path that crosses an array reads as the list of what the elements
    hold — for a goal exactly as for a door. Judged per element by plain
    dot-walk, `includes` on line_items.sku would never see a sku."""
    sku = [Condition(field="payload.line_items.sku", op="includes", value="SKU-2")]
    letter = _event({"line_items": [{"sku": "SKU-1"}, {"sku": "SKU-2"}]})

    assert conditions_match(sku, letter)
    assert not conditions_match(sku, _event({"line_items": []}))
    assert conditions_match([], letter)  # no `where`: the topic alone decides


def test_order_tags_splits_both_shopify_shapes_into_single_tags() -> None:
    """REST's comma string and GraphQL's array read the same, so `includes`
    asks about one tag — and "VIP" never matches "Non-VIP"."""
    rest = _event({"id": 1, "tags": f" {CONFIRMED} ,, COD "})
    graphql = _event({"id": 1, "tags": [CONFIRMED, "COD"]})
    for event in (rest, graphql):
        assert conditions_match(
            [Condition(field="order_tags", op="includes", value=CONFIRMED)], event
        )
    near_miss = _event({"id": 1, "tags": "Non-VIP"})
    assert not conditions_match(
        [Condition(field="order_tags", op="includes", value="VIP")], near_miss
    )
    # No tags is absence, the same as every other op reads it.
    for empty in ("", " , ", [], None):
        untagged = _event({"id": 1, "tags": empty})
        assert conditions_match(
            [Condition(field="order_tags", op="not_exists")], untagged
        ), empty

"""The split square: a stable share of runs down each arm (enh A/04).

The two things worth pinning are the two an experiment dies of: an
assignment that moves when the walker retries (the same customer counted
in both arms), and a share that is not the share the author wrote.
Everything else here is the publish validator refusing structure that
cannot run.
"""

from typing import Any, Dict, List
from uuid import uuid4

import pytest

from app.crm.outreach.nodes import NODE_TYPES
from app.crm.outreach.nodes.context import reply_key, run_facts, split_key
from app.crm.outreach.nodes.split import BUCKETS, arm_for, bucket_of, execute, validate
from app.crm.outreach.schemas import (
    SplitArm,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
)
from app.crm.outreach.walker import pick_next


def _split(node_id: str = "experiment", **shares: int) -> WorkflowNode:
    return WorkflowNode(
        id=node_id,
        type="split",
        arms=[SplitArm(on=on, percent=percent) for on, percent in shares.items()],
    )


def _plan(node: WorkflowNode, edges: List[WorkflowEdge]) -> WorkflowDefinition:
    return WorkflowDefinition(
        **{
            "entry": {"topic": "orders/create"},
            "nodes": [node],
            "edges": edges,
            "goals": [{"topics": ["orders/paid"]}],
            "exits": {"max_age_days": 7},
        }
    )


class _Run:
    """The two fields the square reads."""

    def __init__(self, run_id: str) -> None:
        self.id = run_id
        self.context: Dict[str, Any] = {}


# --- the assignment ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_same_run_lands_in_the_same_arm_every_time() -> None:
    """A lease retry after a crash re-executes the square. If the arm moved,
    an experiment would count one customer in both arms and every number it
    reports would be wrong."""
    node = _split(A=50, B=50)
    plan = _plan(node, [("experiment", "a", "A"), ("experiment", "b", "B")])
    run = _Run(str(uuid4()))
    first = await execute(run, node, plan)  # type: ignore[arg-type]
    for _ in range(1000):
        assert await execute(run, node, plan) == first  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_two_squares_on_one_board_are_independent() -> None:
    """The bucket is keyed on the square as well as the run, so a run is not
    pushed into the 'A' side of both experiments by an accident of its id."""
    plan = _plan(_split(A=50, B=50), [])
    run = _Run("11111111-1111-4111-8111-111111111111")
    both = {
        node_id: (await execute(run, _split(node_id, A=50, B=50), plan))[  # type: ignore[arg-type]
            split_key(node_id)
        ]
        for node_id in ("first", "second", "third", "fourth")
    }
    assert len(set(both.values())) == 2, both


def test_the_shares_are_the_shares_the_author_wrote() -> None:
    """Ten thousand run ids, each arm within three points of its percent."""
    node = _split(control=70, discount=30)
    counts: Dict[str, int] = {"control": 0, "discount": 0}
    for _ in range(10_000):
        counts[arm_for(node.arms, bucket_of(str(uuid4()), node.id))] += 1
    assert abs(counts["control"] / 100 - 70) <= 3, counts
    assert abs(counts["discount"] / 100 - 30) <= 3, counts


def test_every_bucket_lands_on_an_arm() -> None:
    """No `else` exists on a split, so the shares must cover the whole
    hundred — the boundary buckets included."""
    arms = _split(a=25, b=25, c=50).arms
    assert [arm_for(arms, b) for b in (0, 24, 25, 49, 50, 99)] == [
        "a",
        "a",
        "b",
        "b",
        "c",
        "c",
    ]
    assert {arm_for(arms, b) for b in range(BUCKETS)} == {"a", "b", "c"}


# --- what the run carries afterwards ----------------------------------------


@pytest.mark.asyncio
async def test_the_arm_answers_the_edge_and_survives_as_a_fact() -> None:
    """`reply_<node>` is how every branching square answers, so pick_next
    treats a split like a condition; `split_<node>` is the same word kept
    for the report, because the reply is cleared when the token leaves."""
    node = _split(A=100, B=0)
    plan = _plan(node, [("experiment", "a", "A"), ("experiment", "b", "B")])
    run = _Run(str(uuid4()))
    written = await execute(run, node, plan)  # type: ignore[arg-type]

    assert written[reply_key("experiment")] == "A"
    assert written[split_key("experiment")] == "A"
    assert pick_next(node, [("a", "A"), ("b", "B")], written) == "a"
    # A fact, not bookkeeping: a report may group on it, and a template may
    # name it — unlike reply_, which run_facts filters out.
    assert run_facts(written, node).get(split_key("experiment")) == "A"


# --- the validator ----------------------------------------------------------


def test_a_split_needs_two_arms() -> None:
    node = _split(only=100)
    assert validate(node, _plan(node, [("experiment", "a", "only")])) == [
        "split node experiment needs at least two arms — one arm is not a split"
    ]


def test_the_shares_must_be_whole() -> None:
    node = _split(A=50, B=40)
    problems = validate(
        node, _plan(node, [("experiment", "a", "A"), ("experiment", "b", "B")])
    )
    assert problems == [
        "split node experiment: arms add up to 90%, not 100% — every run takes "
        "exactly one arm, so the shares must be whole"
    ]


def test_an_arm_no_run_can_reach_is_refused() -> None:
    """A 0% arm would publish an edge the walker can never take."""
    node = _split(A=100, B=0)
    problems = validate(
        node, _plan(node, [("experiment", "a", "A"), ("experiment", "b", "B")])
    )
    assert problems == [
        "split node experiment: arm 'B' is 0% — an arm no run can reach; "
        "remove it or give it a share"
    ]


def test_an_arm_without_an_edge_is_refused() -> None:
    node = _split(A=50, B=50)
    problems = validate(node, _plan(node, [("experiment", "a", "A")]))
    assert problems == ["split node experiment: arm 'B' has no edge labelled with it"]


def test_a_label_twice_is_refused() -> None:
    node = WorkflowNode(
        id="experiment",
        type="split",
        arms=[SplitArm(on="A", percent=50), SplitArm(on="A", percent=50)],
    )
    problems = validate(node, _plan(node, [("experiment", "a", "A")]))
    assert "split node experiment: label 'A' twice" in problems


@pytest.mark.parametrize("label", ["else", "timeout"])
def test_an_arm_may_not_take_a_walker_word(label: str) -> None:
    with pytest.raises(ValueError, match="the walker owns it"):
        SplitArm(on=label, percent=50)


# --- the registry -----------------------------------------------------------


def test_the_square_is_registered_as_a_branching_word() -> None:
    """pick_next, the reply clearing and the validator all dispatch through
    the registry; a half-added type is a walker that cannot speak what the
    validator accepted."""
    spec = NODE_TYPES["split"]
    assert spec.branches is True
    assert spec.is_wait is False
    assert spec.listens is False
    assert spec.execute is not None

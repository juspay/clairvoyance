"""Goal tiers (rollout phase 06): a goal is a list of tiers, each
{topics, key?, exit_reason}. Tiers are judged keyed-first — the tier that
can say "THIS cart was recovered" beats the customer-level one that only
says "she bought something" — and the first tier that matches a run ends
it with that tier's reason. The singular `goal` is still accepted and
becomes one tier, so every published document keeps validating."""

from typing import Any, Dict

from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import WorkflowDefinition

_BASE: Dict[str, Any] = {
    "entry": {"topic": "checkouts/update", "reenter": True, "cooldown_hours": 0},
    "nodes": [{"id": "wait-30m", "type": "wait", "minutes": 30}],
    "edges": [],
}
RECOVERED = {
    "topics": ["orders/create", "orders/paid"],
    "key": {"event": "cart_token", "run": "cart_token"},
    "exit_reason": "goal_met",
}
ELSEWHERE = {
    "topics": ["orders/create", "orders/paid"],
    "exit_reason": "converted_elsewhere",
}


def test_the_singular_goal_still_validates_as_one_tier() -> None:
    definition = WorkflowDefinition.model_validate(
        {**_BASE, "goal": {"topics": ["orders/create"]}}
    )
    (tier,) = definition.goals
    assert tier.topics == ["orders/create"]
    assert tier.key is None and tier.exit_reason == "goal_met"
    assert validate_definition({**_BASE, "goal": {"topics": ["orders/create"]}}) == []


def test_two_tiers_recovered_then_converted_elsewhere() -> None:
    raw = {**_BASE, "goals": [RECOVERED, ELSEWHERE]}
    assert validate_definition(raw) == []
    definition = WorkflowDefinition.model_validate(raw)
    assert [t.exit_reason for t in definition.goals] == [
        "goal_met",
        "converted_elsewhere",
    ]
    assert definition.goals[0].key is not None
    assert definition.goals[0].key.event == "cart_token"


def test_goal_and_goals_together_is_a_shape_error() -> None:
    problems = validate_definition(
        {**_BASE, "goal": {"topics": ["a"]}, "goals": [{"topics": ["b"]}]}
    )
    assert problems and "shape invalid" in problems[0]


def test_two_tiers_with_the_same_exit_reason_are_refused() -> None:
    problems = validate_definition(
        {**_BASE, "goals": [{"topics": ["a"]}, {"topics": ["b"]}]}
    )
    assert any("exit_reason" in p and "goal_met" in p for p in problems)


def test_an_exit_reason_outside_the_goal_vocabulary_is_refused() -> None:
    for bad in ("recovered", "completed", "timed_out", "ejected"):
        problems = validate_definition(
            {**_BASE, "goals": [{"topics": ["a"], "exit_reason": bad}]}
        )
        assert any("exit_reason" in p for p in problems), bad
    assert (
        validate_definition(
            {
                **_BASE,
                "goals": [{"topics": ["loan.rejected"], "exit_reason": "withdrawn"}],
            }
        )
        == []
    )


def test_tiers_are_judged_keyed_first_in_document_order() -> None:
    """The document lists the customer-level tier FIRST here; the keyed
    tier must still be judged first — it is the more specific claim, and a
    run it ends is no longer open for the unkeyed tier to catch."""
    definition = WorkflowDefinition.model_validate(
        {**_BASE, "goals": [ELSEWHERE, RECOVERED]}
    )
    assert [t.exit_reason for t in definition.goal_tiers()] == [
        "goal_met",
        "converted_elsewhere",
    ]
    assert [t.exit_reason for t in definition.goal_tiers("orders/paid")] == [
        "goal_met",
        "converted_elsewhere",
    ]
    assert definition.goal_tiers("orders/refunded") == []

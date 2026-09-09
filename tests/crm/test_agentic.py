"""agentic module: translation (Juspay vocabulary -> ours), fail-closed action
pick, and every draw-admission refusal reason."""

from app.crm.preview.uap.contracts import derive_status, pick_action, plan_patch


def _juspay_agent(status="ACTIVE", actions=None, **extra):
    return {"status": status, "actions": actions or [], "agent_id": "cont_a", **extra}


def test_pick_action_matches_our_ref_only() -> None:
    ours = {"object_reference_id": "intent_x_1", "status": "ACTIVE", "action_id": "c1"}
    foreign = {
        "object_reference_id": "intent_other",
        "status": "ACTIVE",
        "action_id": "c2",
    }
    assert pick_action(_juspay_agent(actions=[foreign, ours]), "intent_x_1") is ours
    assert pick_action(_juspay_agent(actions=[foreign]), "intent_x_1") is None
    assert pick_action(_juspay_agent(actions=[ours]), None) is None


def test_derive_status_needs_both_active() -> None:
    assert derive_status(None, None) == "EXPIRED"
    assert derive_status(_juspay_agent("PAUSED"), {"status": "ACTIVE"}) == "PAUSED"
    assert derive_status(_juspay_agent("REVOKED"), {"status": "ACTIVE"}) == "REVOKED"
    assert derive_status(_juspay_agent("ACTIVE"), None) == "PENDING"
    assert derive_status(_juspay_agent("ACTIVE"), {"status": "EXPIRED"}) == "EXPIRED"
    assert derive_status(_juspay_agent("ACTIVE"), {"status": "REVOKED"}) == "REVOKED"
    assert derive_status(_juspay_agent("ACTIVE"), {"status": "FAILED"}) == "FAILED"
    assert derive_status(_juspay_agent("ACTIVE"), {"status": "ACTIVE"}) == "ACTIVE"
    assert (
        derive_status(_juspay_agent("ACTIVE"), {"status": "SOMETHING_NEW"}) == "PENDING"
    )


def test_plan_patch_keeps_rider_decision_and_takes_approved_rule() -> None:
    action = {
        "status": "ACTIVE",
        "action_id": "c1",
        "action_ref_id": "ARID-1",
        "intent_constraints": {"max_per_draw": "500.00"},
    }
    patch = plan_patch("PENDING", _juspay_agent(payer_avpa="a@upi"), action)
    assert patch["status"] == "ACTIVE"
    assert patch["payer_avpa"] == "a@upi"
    assert patch["intent_constraints"] == {"max_per_draw": "500.00"}
    # PAUSED by the rider stays PAUSED unless Juspay says ACTIVE again
    assert plan_patch("PAUSED", _juspay_agent("ACTIVE"), None)["status"] == "PAUSED"
    assert plan_patch("PAUSED", _juspay_agent("ACTIVE"), action)["status"] == "ACTIVE"
    assert plan_patch("PENDING", None, None)["status"] == "EXPIRED"
    # an agent that is GONE is expired whatever the rider had chosen
    assert plan_patch("PAUSED", None, None)["status"] == "EXPIRED"


def test_plan_patch_inactive_is_final() -> None:
    """Deleted by the rider here: no Juspay state can bring it back."""
    action = {"status": "ACTIVE", "action_id": "c1"}
    assert (
        plan_patch("INACTIVE", _juspay_agent("ACTIVE"), action)["status"] == "INACTIVE"
    )
    assert plan_patch("INACTIVE", _juspay_agent("ACTIVE"), None)["status"] == "INACTIVE"
    assert plan_patch("INACTIVE", _juspay_agent("PAUSED"), None)["status"] == "INACTIVE"
    assert plan_patch("INACTIVE", None, None)["status"] == "INACTIVE"
    # identifiers from Juspay still land; only the status is pinned
    patch = plan_patch("INACTIVE", _juspay_agent("ACTIVE", payer_avpa="a@upi"), action)
    assert patch["payer_avpa"] == "a@upi" and patch["action_id"] == "c1"
    # and INACTIVE is never derived from Juspay's vocabulary
    assert derive_status(_juspay_agent("ACTIVE"), action) == "ACTIVE"


from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.crm.preview.uap.contracts import check_draw, remaining
from app.crm.preview.uap.schemas import CrmCustomerAgent, DrawUsage


def _agent(**over) -> CrmCustomerAgent:
    base = dict(
        id="11111111-1111-1111-1111-111111111111",
        merchant_id="m",
        customer_id="22222222-2222-2222-2222-222222222222",
        agent_obj_ref="agent_x_1",
        agent_id="cont_a",
        action_id="cont_x",
        payer_avpa="a@upi",
        status="ACTIVE",
        action_status="ACTIVE",
        intent_constraints={
            "max_per_draw": "500.00",
            "max_total": "1000.00",
            "max_draws": 3,
            # 10-digit epoch seconds — the current wire/stored form.
            "valid_till": str(
                int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
            ),
        },
    )
    base.update(over)
    return CrmCustomerAgent(**base)


def test_allowed() -> None:
    assert check_draw(_agent(), DrawUsage(), "27.00") is None


def test_every_refusal_reason() -> None:
    assert check_draw(_agent(action_id=None), DrawUsage(), "27.00") == "action_missing"
    assert check_draw(_agent(status="PAUSED"), DrawUsage(), "27.00") == "agent_inactive"
    assert (
        check_draw(_agent(action_status="EXPIRED"), DrawUsage(), "27.00")
        == "action_inactive"
    )
    assert (
        check_draw(_agent(intent_constraints=None), DrawUsage(), "27.00")
        == "limit_unverifiable"
    )
    assert check_draw(_agent(), DrawUsage(), "abc") == "invalid_amount"
    assert check_draw(_agent(), DrawUsage(), "600.00") == "over_limit"
    assert (
        check_draw(_agent(), DrawUsage(drawn_total=Decimal("990.00")), "27.00")
        == "total_exhausted"
    )
    assert check_draw(_agent(), DrawUsage(draw_count=3), "27.00") == "draws_exhausted"
    # 13-digit milliseconds on purpose — legacy stored rows must keep parsing.
    past = str(int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000))
    stale = _agent(
        intent_constraints={**(_agent().intent_constraints or {}), "valid_till": past}
    )
    assert check_draw(stale, DrawUsage(), "27.00") == "expired"
    future = str(int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp()))
    early = _agent(
        intent_constraints={**(_agent().intent_constraints or {}), "valid_from": future}
    )
    assert check_draw(early, DrawUsage(), "27.00") == "not_yet_valid"


def test_remaining() -> None:
    view = remaining(_agent(), DrawUsage(drawn_total=Decimal("100.00"), draw_count=1))
    assert view["remaining_total"] == "900.00"
    assert view["remaining_draws"] == 2
    assert view["max_per_draw"] == "500.00"

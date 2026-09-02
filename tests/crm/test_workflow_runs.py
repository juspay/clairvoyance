"""Run-operations laws: resume touches only parked rows, the sweep only
old exited rows (batched), the list is merchant-first."""

from datetime import datetime, timezone

import pytest

from app.crm.outreach.db.queries.enrollment import (
    list_runs_query,
    resume_run_query,
    sweep_exited_runs_query,
)
from app.crm.outreach.nodes import lead_request_id, run_facts, send_variables
from app.crm.outreach.runs import list_runs
from app.crm.outreach.schemas import WorkflowNode
from app.crm.outreach.walker import retry_delay_seconds

NOW = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)


def test_resume_touches_only_parked_runs_of_this_merchant() -> None:
    sql, params = resume_run_query("m1", "wf-1", "en-1")
    assert "status = 'parked'" in sql
    assert "merchant_id = $1" in sql and params[0] == "m1"
    assert "attempts = 0" in sql and "wake_at = now()" in sql
    # last_error deliberately survives (the operator sees what they
    # fixed) — the SET clause must not touch it; RETURNING may list it
    set_clause = sql.split("WHERE")[0]
    assert "last_error" not in set_clause


def test_sweep_deletes_only_old_exited_rows_batched() -> None:
    sql, params = sweep_exited_runs_query(NOW, 500)
    assert "status = 'exited'" in sql
    assert "exited_at < $1" in sql
    assert "LIMIT $2" in sql  # batched — never a long lock
    assert params == [NOW, 500]


def test_list_runs_is_merchant_first_with_optional_status() -> None:
    sql, params = list_runs_query("m1", "wf-1", None, 50, 0)
    assert "merchant_id = $1" in sql and params == ["m1", "wf-1", 50, 0]
    sql, params = list_runs_query("m1", "wf-1", "parked", 50, 0)
    assert "status = $3" in sql and params == ["m1", "wf-1", "parked", 50, 0]


@pytest.mark.asyncio
async def test_list_runs_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        await list_runs("m1", "wf-1", "vanished", 50, 0)


def test_retry_delay_doubles_from_the_lease_and_stays_within_jitter() -> None:
    for attempts, base_expected in ((1, 300), (2, 600), (3, 1200), (20, 3600)):
        delay = retry_delay_seconds(attempts, 300)
        assert round(base_expected * 0.8) <= delay <= round(base_expected * 1.2)


def test_call_payload_and_send_variables_share_one_bookkeeping_filter() -> None:
    context = {
        "source_event_id": "e-1",
        "phone": "+91",
        "customer_mobile_number": "+91",
        "lead_call": "l-1",
        "message_ask": "m-1",
        "reply_ask": "YES",
        "reporting_webhook_url": "https://merchant/report",
        "cart_value": 1999,
        "repeat_event_ids": ["e-2", "e-3"],
        "repeat_items": [{"order_id": "B"}],
        "repeat_count": 3,
    }
    facts = run_facts(context)
    assert facts == {
        "reporting_webhook_url": "https://merchant/report",
        "cart_value": 1999,
        "repeat_count": 3,
    }
    # The send posts EXACTLY what the node mapped, {blank: fact} — nothing
    # rides along, whatever else the context holds.
    assert send_variables({"1": "cart_value", "n": "repeat_count"}, context) == {
        "1": 1999,
        "n": 3,
    }
    assert send_variables({}, context) == {}
    with pytest.raises(KeyError):
        send_variables({"1": "cart_value", "2": "coupon"}, context)


def test_lead_request_id_is_the_merchants_order_id_else_the_run() -> None:
    assert lead_request_id({"order_id": "o-1001"}, "r-1") == "o-1001"
    assert lead_request_id({"order_id": 4567}, "r-1") == "4567"
    assert lead_request_id({"request_id": "req-9"}, "r-1") == "req-9"
    assert lead_request_id({"order_id": ""}, "r-1") == "wf-r-1"
    assert lead_request_id({}, "r-1") == "wf-r-1"
    # A keyed plan: the enrollment key IS the order id (Shopify's `id`) —
    # it beats every payload key, so nautilus matches the outcome to the order.
    assert lead_request_id({"order_id": "o-1001"}, "r-1", "5306070030") == "5306070030"
    assert lead_request_id({}, "r-1", None) == "wf-r-1"


def test_the_current_squares_facts_win_and_every_squares_stay_reachable() -> None:
    """Phase 16: a letter's facts land under facts.<square>. For the
    template the current square's facts override the top-level ones (the
    most recent stage wins), every square's stay reachable as
    facts_<square>_<key>, the square itself rides as current_node (and
    current_stage when labelled), and `facts` is bookkeeping."""
    context = {
        "phone": "+91",
        "amount": 100,
        "facts": {
            "at-kyc": {"amount": 250, "doc": "pan"},
            "at-bank": {"bank": "hdfc"},
        },
    }
    node = WorkflowNode(
        id="at-kyc",
        type="wait_event",
        topics=["loan.bank_linked"],
        key="$topic",
        minutes=30,
        stage="kyc",
    )
    facts = run_facts(context, node)
    assert facts["amount"] == 250 and facts["doc"] == "pan"
    assert facts["facts_at-kyc_amount"] == 250 and facts["facts_at-bank_bank"] == "hdfc"
    assert facts["current_node"] == "at-kyc" and facts["current_stage"] == "kyc"
    assert "facts" not in facts and "phone" not in facts
    plain = run_facts(context)
    assert (
        plain["amount"] == 100 and "current_node" not in plain and "facts" not in plain
    )
    assert plain["facts_at-bank_bank"] == "hdfc"
    unlabelled = WorkflowNode(id="w", type="wait", minutes=1)
    assert "current_stage" not in run_facts(context, unlabelled)
    # a send blank may name the square, its stage, or a stage letter's fact
    assert send_variables(
        {"1": "current_node", "2": "current_stage", "3": "facts_at-bank_bank"},
        context,
        node,
    ) == {"1": "at-kyc", "2": "kyc", "3": "hdfc"}


def test_send_variables_refuse_what_a_provider_cannot_render() -> None:
    """A bool or a None posted as a variable makes the WhatsApp face refuse
    the message terminally — so a MAPPED one parks the run here, by name,
    instead of posting it. The call payload is not narrowed — the lead
    machine spells its own."""
    context = {"name": "Priya", "amount": 1999, "vip": True, "note": None, "score": 4.5}
    assert send_variables({"1": "name", "2": "amount", "3": "score"}, context) == {
        "1": "Priya",
        "2": 1999,
        "3": 4.5,
    }
    for fact in ("vip", "note"):
        with pytest.raises(ValueError, match=fact):
            send_variables({"1": fact}, context)
    assert run_facts(context)["vip"] is True


def test_the_latest_letters_facts_win_when_the_square_that_heard_it_is_behind() -> None:
    """Phase 17: on a ladder the letter that moves the run is heard on the
    square it LEAVES, and the action then executes as its own square, so
    the current-square override never fires — a KYC-stage call would read
    the founding letter's `stage`. The consumer points at the square that
    heard the latest letter (latest_letter, bookkeeping); its facts overlay
    the founding letter's — the most recent letter wins — and the current
    square's own still win over both."""
    context = {
        "stage": "profile_created",
        "amount": 100,
        "facts": {"at-profile": {"stage": "kyc_completed", "amount": 250}},
        "latest_letter": "at-profile",
    }
    act = WorkflowNode(id="act-kyc", type="call", template_id="t", stage="loan.kyc")
    facts = run_facts(context, act)
    assert facts["stage"] == "kyc_completed" and facts["amount"] == 250
    assert facts["current_stage"] == "loan.kyc" and "latest_letter" not in facts
    assert facts["facts_at-profile_stage"] == "kyc_completed"
    assert run_facts(context)["stage"] == "kyc_completed"  # with no square given too
    # the current square's own letter still wins over the latest one
    context["facts"]["act-kyc"] = {"amount": 999}
    assert run_facts(context, act)["amount"] == 999
    assert run_facts(context, act)["stage"] == "kyc_completed"
    # a pointer at a square with no letter changes nothing
    assert run_facts({**context, "latest_letter": "gone"})["amount"] == 100
    assert "latest_letter" not in run_facts(context, act)
    assert send_variables({"1": "amount"}, context, act) == {"1": 999}

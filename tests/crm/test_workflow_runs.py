"""Run-operations laws: resume touches only parked rows, the sweep only
old exited rows (batched), the list is merchant-first."""

from datetime import datetime, timezone

import pytest

from app.crm.outreach.db.queries import (
    list_runs_query,
    resume_run_query,
    sweep_exited_runs_query,
)
from app.crm.outreach.nodes import lead_request_id, run_facts, send_variables
from app.crm.outreach.runs import list_runs
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
    }
    facts = run_facts(context)
    assert facts == {
        "reporting_webhook_url": "https://merchant/report",
        "cart_value": 1999,
    }
    assert send_variables(context) == {"cart_value": 1999}


def test_lead_request_id_is_the_merchants_order_id_else_the_run() -> None:
    assert lead_request_id({"order_id": "o-1001"}, "r-1") == "o-1001"
    assert lead_request_id({"order_id": 4567}, "r-1") == "4567"
    assert lead_request_id({"request_id": "req-9"}, "r-1") == "req-9"
    assert lead_request_id({"order_id": ""}, "r-1") == "wf-r-1"
    assert lead_request_id({}, "r-1") == "wf-r-1"

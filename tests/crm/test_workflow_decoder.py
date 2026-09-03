"""Row -> shape translation for the outreach module, including the
jsonb-as-text defensive path (the record-module precedent)."""

import json
from datetime import datetime, timezone
from uuid import uuid4

from app.crm.outreach.db.decoders.enrollment import decode_run
from app.crm.outreach.db.decoders.workflow import decode_workflow

NOW = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)


def test_decode_run_parses_string_context() -> None:
    run_id, wf_id, cust_id = uuid4(), uuid4(), uuid4()
    row = {
        "id": run_id,
        "merchant_id": "m1",
        "workflow_id": wf_id,
        "workflow_version": 3,
        "customer_id": cust_id,
        "status": "waiting",
        "current_node": "wait-30m",
        "wake_at": NOW,
        "entered_at": NOW,
        "exited_at": None,
        "exit_reason": None,
        "context": json.dumps({"source_event_id": "e-501", "phone": "+91"}),
        "enrollment_key": str(cust_id),
        "attempts": 0,
        "last_error": None,
    }
    run = decode_run(row)
    assert run.context["phone"] == "+91"
    assert run.customer_id == cust_id
    assert run.status == "waiting"


def test_decode_workflow_carries_both_documents() -> None:
    wf_id = uuid4()
    definition = {"entry": {"topic": "t"}, "nodes": []}
    row = {
        "id": wf_id,
        "merchant_id": "m1",
        "name": "checkout-rescue",
        "status": "live",
        "version": 3,
        "created_by": "ops@x",
        "created_at": NOW,
        "updated_at": NOW,
        "definition": json.dumps(definition),
        "draft": None,
    }
    workflow = decode_workflow(row)
    assert workflow.definition == definition
    assert workflow.draft is None
    assert workflow.version == 3

"""Row -> domain shapes for the outreach module (module rules §1).
DB-side translation only — never imported outside db/."""

import json
from typing import Any, Mapping

from app.crm.outreach.schemas import EnrollmentRun, Workflow, WorkflowSummary


def _jsonb(value: Any) -> Any:
    """asyncpg hands jsonb back as text unless a codec is registered —
    decode defensively, the record-module precedent."""
    if isinstance(value, str):
        return json.loads(value)
    return value


def decode_workflow_summary(row: Mapping[str, Any]) -> WorkflowSummary:
    return WorkflowSummary(
        id=row["id"],
        merchant_id=row["merchant_id"],
        name=row["name"],
        status=row["status"],
        version=row["version"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_workflow(row: Mapping[str, Any]) -> Workflow:
    return Workflow(
        **decode_workflow_summary(row).model_dump(),
        definition=_jsonb(row["definition"]),
        draft=_jsonb(row["draft"]),
    )


def decode_run(row: Mapping[str, Any]) -> EnrollmentRun:
    return EnrollmentRun(
        id=row["id"],
        merchant_id=row["merchant_id"],
        workflow_id=row["workflow_id"],
        workflow_version=row["workflow_version"],
        customer_id=row["customer_id"],
        status=row["status"],
        current_node=row["current_node"],
        wake_at=row["wake_at"],
        entered_at=row["entered_at"],
        exited_at=row["exited_at"],
        exit_reason=row["exit_reason"],
        context=_jsonb(row["context"]) or {},
        enrollment_key=row["enrollment_key"],
        attempts=row["attempts"],
        last_error=row["last_error"],
    )

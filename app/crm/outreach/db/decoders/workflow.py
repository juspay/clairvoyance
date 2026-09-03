"""row -> schema translation for crm_workflow (T19) — one table, one file (module rules §1 at scale;
outreach took the shape 3 Sep 2026, structure PR 2). DB-side translation only — never
imported outside db/.
"""

from typing import Any, Mapping

from app.crm.outreach.schemas import (
    Workflow,
    WorkflowSummary,
)
from app.crm.shared.decode import jsonb_value as _jsonb


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

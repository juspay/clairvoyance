"""row -> schema translation for crm_workflow_version (T25, ADR 0023) — one table, one file (module rules §1 at scale;
outreach took the shape 3 Sep 2026, structure PR 2). DB-side translation only — never
imported outside db/.
"""

from typing import Any, Mapping

from app.crm.outreach.schemas import (
    WorkflowVersion,
)


def decode_version(row: Mapping[str, Any]) -> WorkflowVersion:
    return WorkflowVersion(
        version=row["version"],
        on_publish=row["on_publish"],
        published_by=row["published_by"],
        published_at=row["published_at"],
        open_runs=int(row["open_runs"] or 0),
    )

"""row -> schema translation for crm_workflow_step (T26) — one table, one file. DB-side
translation only — never imported outside db/.
"""

from typing import Any, Mapping

from app.crm.outreach.schemas import RunStep


def decode_step(row: Mapping[str, Any]) -> RunStep:
    """One CLOSED square (canon T26). ``left_at`` is NOT NULL in the table
    — a None on this model is the OPEN square steps.timeline() appends from
    the run row, which never comes from here."""
    return RunStep(
        node=row["node"],
        node_type=row["node_type"],
        arrived_at=row["arrived_at"],
        left_at=row["left_at"],
        arrived_by=row["arrived_by"],
        outcome=row["outcome"],
        next_node=row["next_node"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        dispatch_id=row["dispatch_id"],
        cut_short_by=row["cut_short_by"],
        workflow_version=row["workflow_version"],
    )

"""row -> schema translation for crm_workflow_enrollment (T20) — one table, one file (module rules §1 at scale;
outreach took the shape 3 Sep 2026, structure PR 2). DB-side translation only — never
imported outside db/.
"""

from typing import Any, Dict, Iterable, Mapping, Optional

from app.crm.outreach.schemas import (
    CustomerRun,
    EnrollmentRun,
    WorkflowRunSummary,
)
from app.crm.shared.decode import jsonb_value as _jsonb


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


def decode_customer_run(row: Mapping[str, Any]) -> CustomerRun:
    return CustomerRun(
        **decode_run(row).model_dump(), workflow_name=row["workflow_name"]
    )


def _number(value: Any) -> Optional[float]:
    """Total: a numeric/Decimal aggregate as a float, NULL as None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def decode_run_summary(rows: Iterable[Mapping[str, Any]]) -> WorkflowRunSummary:
    """Fold workflow_summary_query's grouping-set rows into one summary.
    grouping_level 0 rows are one (status, exit_reason) each; the level-3
    row (both columns grouped away) is the whole window. Total: an empty
    window is a zero summary, never a raise."""
    runs = 0
    by_exit_reason: Dict[str, int] = {}
    open_runs = {"waiting": 0, "parked": 0}
    median: Optional[float] = None
    recovered: Optional[float] = None
    for row in rows:
        if int(row["grouping_level"] or 0) == 3:
            runs = int(row["runs"] or 0)
            median = _number(row["median_minutes_to_exit"])
            recovered = _number(row["recovered_amount"])
            continue
        status, reason = row["status"], row["exit_reason"]
        if status in open_runs:
            open_runs[status] += int(row["runs"] or 0)
        elif reason:
            by_exit_reason[reason] = by_exit_reason.get(reason, 0) + int(
                row["runs"] or 0
            )
    return WorkflowRunSummary(
        runs=runs,
        by_exit_reason=by_exit_reason,
        open=open_runs,
        median_minutes_to_exit=median,
        recovered_amount=recovered,
    )

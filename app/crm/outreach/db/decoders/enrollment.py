"""row -> schema translation for crm_workflow_enrollment (T20) — one table, one file (module rules §1 at scale;
outreach took the shape 3 Sep 2026, structure PR 2). DB-side translation only — never
imported outside db/.
"""

from typing import Any, Dict, Iterable, Mapping, Optional

from app.crm.outreach.schemas import (
    SPLIT_PREFIX,
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


def decode_split_counts(
    rows: Iterable[Mapping[str, Any]],
) -> Dict[str, Dict[str, int]]:
    """Fold workflow_split_counts_query's rows into {node: {arm: count}}
    (enh A/04). The key carries the prefix the context stores it under;
    the report names the SQUARE, which is what an author drew. Total: a
    row whose key is only the prefix is skipped rather than filed under
    an empty node id."""
    by_split: Dict[str, Dict[str, int]] = {}
    for row in rows:
        node_id = str(row["arm_key"] or "")[len(SPLIT_PREFIX) :]
        arm = row["arm"]
        if not node_id or arm is None:
            continue
        by_split.setdefault(node_id, {})[str(arm)] = int(row["runs"] or 0)
    return by_split


def decode_run_summary(
    rows: Iterable[Mapping[str, Any]],
    split_rows: Optional[Iterable[Mapping[str, Any]]] = None,
) -> WorkflowRunSummary:
    """Fold workflow_summary_query's grouping-set rows into one summary.
    grouping_level 0 rows are one (status, exit_reason) each; the level-3
    row (both columns grouped away) is the whole window. Total: an empty
    window is a zero summary, never a raise.

    ``split_rows`` is the second statement's own rows (enh A/04), optional
    because a plan with no split square has none and a caller reading only
    the aggregate should not have to say so."""
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
        by_split=decode_split_counts(split_rows or ()),
    )

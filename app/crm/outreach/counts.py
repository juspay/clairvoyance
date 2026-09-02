"""Run counting — what a pile of runs means, at two scales.

Each read is a pair: run_* fetches the grouped rows, fold_* tallies them.
Both fold through _tally, so the plan list and the run header cannot
disagree. Folding is a decision, so it is here and not in db/.
"""

from typing import Any, Dict, List, Mapping

from app.crm.outreach.db import accessor
from app.crm.outreach.schemas import RunCounts

_GOAL_MET = "goal_met"


def _tally(counts: RunCounts, row: Mapping[str, Any]) -> None:
    """PURE: add one grouped (status, exit_reason, n) row to an accumulator.

    goal_met is an exit REASON, not a status — counted inside `exited` too,
    which makes the rate a share of the runs that finished. Every row lands
    in `total`, so a status this build has not heard of still shows up.
    """
    n = int(row["n"])
    counts.total += n
    status = row["status"]
    if status == "waiting":
        counts.waiting += n
    elif status == "parked":
        counts.parked += n
    elif status == "exited":
        counts.exited += n
    if row.get("exit_reason") == _GOAL_MET:
        counts.goal_met += n


async def run_counts(merchant_id: str, workflow_id: str) -> List[Dict[str, Any]]:
    """This flow's runs grouped by (status, exit_reason), raw."""
    return await accessor.run_counts(merchant_id, workflow_id)


async def fold_counts(merchant_id: str, workflow_id: str) -> RunCounts:
    """The run header's numbers — `exited` arrives split one row per
    reason, and the tally adds them back together."""
    counts = RunCounts()
    for row in await run_counts(merchant_id, workflow_id):
        _tally(counts, row)
    return counts


async def run_counts_all(merchant_id: str) -> List[Dict[str, Any]]:
    """Every plan's runs grouped by (workflow_id, status, exit_reason) in
    one read, raw — otherwise it is a round trip per row of the list."""
    return await accessor.run_counts_all(merchant_id)


async def fold_counts_by_workflow(merchant_id: str) -> Dict[str, RunCounts]:
    """Counts per plan — the list's ACTIVE / PARKED / GOAL MET columns.

    A plan with no runs has no row to tally, so it is ABSENT from the map
    rather than present with zeros.
    """
    by_plan: Dict[str, RunCounts] = {}
    for row in await run_counts_all(merchant_id):
        _tally(by_plan.setdefault(str(row["workflow_id"]), RunCounts()), row)
    return by_plan

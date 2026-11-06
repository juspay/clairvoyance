"""Version operations (ADR 0023, rollout phase 14) — what an operator does
with the versions a plan has published.

  migrate_forward    every open run pinned to version A now executes
                     version B — how a typo fix reaches runs in flight on a
                     pin-mode plan. Guarded by validate_migration (plans.py:
                     the stranding laws as a pure function): B must keep
                     every square those runs stand on and mean the same at
                     the entry. One atom — the occupied-square read and the
                     re-pin share one fate.
  list_versions      each published document with how many open runs still
                     execute it: what to migrate from.
  template_references
                     what connectivity's retire asks through the hook
                     worker_main registers (connectivity may not import
                     outreach): open runs whose PINNED document has a send
                     node naming this template, and live or paused plans
                     whose LATEST document does — the next entrant would be
                     stranded just like a run in flight.

Versions are never deleted (ADR 0023 §5, amended 2026-09-03): a row is one
small document, a plan publishes tens of them, every read is a point
lookup, and an exited run's workflow_version must keep answering "what
did this run execute". There is no sweep.

gather -> decide (PURE, in plans.py) -> apply.
"""

from typing import List, Tuple

from app.core.logger import logger
from app.crm.outreach import plans
from app.crm.outreach.db import DbTxn, atomically
from app.crm.outreach.db.accessors import (
    enrollment as enrollment_accessor,
    version as version_accessor,
    workflow as workflow_accessor,
)
from app.crm.outreach.plans import WorkflowValidationError, validate_migration
from app.crm.outreach.schemas import WorkflowDefinition, WorkflowVersion


class VersionNotFound(Exception):
    """No such version row for this plan (the route's 404)."""


async def migrate_forward(
    merchant_id: str, workflow_id: str, from_version: int, to_version: int
) -> int:
    """Move every open run pinned to from_version under to_version. Returns
    how many moved. Refused (WorkflowValidationError) when the target would
    strand them; VersionNotFound when either version does not exist."""
    return await atomically(
        _migrate_forward_in_txn, merchant_id, workflow_id, from_version, to_version
    )


async def _migrate_forward_in_txn(
    txn: DbTxn, merchant_id: str, workflow_id: str, from_version: int, to_version: int
) -> int:
    """ATOMIC: the occupied-square read, the target's template check and
    the re-pin share one fate — a refused migration writes nothing, the
    squares judged are the squares at the moment of the move, and the
    templates the target sends are held SHARED (shared/locks.py) from
    before the approval check so a retirement cannot slip in between.
    READ COMMITTED, no row lock: a walker mid-visit may still advance a
    run onto a square the target lacks — the same window the migrate-mode
    publish has — and the consequence is an honest park at its next
    claim, never a silent wrong step."""
    if from_version == to_version:
        raise WorkflowValidationError(
            [f"version {from_version} is already the version those runs execute"]
        )
    source = await version_accessor.pinned_definition(
        txn, merchant_id, workflow_id, from_version
    )
    if source is None:
        raise VersionNotFound(f"version {from_version}")
    target = await version_accessor.pinned_definition(
        txn, merchant_id, workflow_id, to_version
    )
    if target is None:
        raise VersionNotFound(f"version {to_version}")
    occupied = await enrollment_accessor.occupied_nodes_on_version(
        txn, merchant_id, workflow_id, from_version
    )
    problems = validate_migration(source, target, occupied)
    if problems:
        raise WorkflowValidationError(problems)
    # The target is an old version: what was approved at its publish may
    # have been retired since. The same check publish makes, under the
    # same lock — a run must never be moved onto a template it cannot send.
    target_definition = WorkflowDefinition.model_validate(target)
    await version_accessor.lock_templates_shared(
        txn, merchant_id, target_definition.send_templates()
    )
    problems = await plans._template_problems(merchant_id, target_definition)
    if problems:
        raise WorkflowValidationError(problems)
    moved = await enrollment_accessor.repin_runs_on_version(
        txn, merchant_id, workflow_id, from_version, to_version
    )
    logger.info(
        f"workflow {workflow_id}: {moved} open runs migrated "
        f"v{from_version} -> v{to_version} (merchant {merchant_id})"
    )
    return moved


async def list_versions(merchant_id: str, workflow_id: str) -> List[WorkflowVersion]:
    """Every published document of the plan, newest first, each with the
    open runs still executing it."""
    return await version_accessor.list_versions(merchant_id, workflow_id)


async def template_references(
    merchant_id: str, channel: str, name: str
) -> Tuple[int, int]:
    """Who would still send this template: (open runs, by each run's
    PINNED document — the one the walker will actually execute; live or
    paused plans, by their LATEST document — the one their next entrant
    is pinned to). Connectivity's retire asks this inside its withdrawal
    atom, through the hook worker_main registers."""
    open_runs = await enrollment_accessor.runs_referencing_template(
        merchant_id, channel, name
    )
    plans = await workflow_accessor.live_plans_naming_template(
        merchant_id, channel, name
    )
    return open_runs, plans

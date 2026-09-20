"""/crm/workflows — the plans surface (W1).

Thin routes per module rules §1: the tenancy door as each route's DECLARED
dependency, delegate to plans.py / runs.py / analytics.py / versions.py.

Auth is the RBAC bearer JWT plus the tenancy check in ``app.crm.auth`` —
not ``crm_admin_user``, which needs the platform-admin role and would lock
merchants out of their own workflows. Same move the connectors family made
(an early departure from ADR 0007's admin-only phase 1); admins still pass.

Every route declares ``merchant_scope(...)`` — it finds the merchant, runs
the check, sets the log context — so a route cannot forget it; a test walks
the routers to keep it that way. Create and publish also take the plain RBAC
dependency for the caller's email: a byline, not a door.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.auth import merchant_scope
from app.crm.outreach import analytics, plans, runs, versions
from app.crm.outreach.schemas import (
    CustomerRun,
    EnrollmentRun,
    PublishCheck,
    RunAnchor,
    RunCall,
    RunPage,
    RunStep,
    VersionMigration,
    Workflow,
    WorkflowCallSummary,
    WorkflowReport,
    WorkflowRunSummary,
    WorkflowSummary,
    WorkflowVersion,
)
from app.schemas import UserInfo

router = APIRouter()
# The customer-facing door (/customers/{id}/runs) mounts under a different
# prefix than the plans' router — the record module's two-router precedent.
customer_router = APIRouter()


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    definition: Dict[str, Any]


class WorkflowStatusChange(BaseModel):
    status: Literal["live", "paused", "archived"]


@router.post("", response_model=Workflow, status_code=status.HTTP_201_CREATED)
async def create_workflow_route(
    body: WorkflowCreate,
    merchant_id: str = Depends(
        merchant_scope("create a workflow", "crm.workflows.create")
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> Workflow:
    try:
        return await plans.create_workflow(
            merchant_id, body.name, body.definition, current_user.email
        )
    except plans.WorkflowValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.problems
        )


@router.get("", response_model=List[WorkflowSummary])
async def list_workflows_route(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    merchant_id: str = Depends(merchant_scope("list workflows", "crm.workflows.list")),
) -> List[WorkflowSummary]:
    return await plans.list_workflows(merchant_id, limit, offset)


@router.get("/{workflow_id}", response_model=Workflow)
async def get_workflow_route(
    workflow_id: str,
    merchant_id: str = Depends(merchant_scope("read a workflow", "crm.workflows.get")),
) -> Workflow:
    workflow = await plans.get_workflow(merchant_id, workflow_id)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found"
        )
    return workflow


@router.put("/{workflow_id}/draft", response_model=Workflow)
async def update_draft_route(
    workflow_id: str,
    body: WorkflowCreate,
    merchant_id: str = Depends(
        merchant_scope("edit a workflow draft", "crm.workflows.draft")
    ),
) -> Workflow:
    try:
        workflow = await plans.update_draft(merchant_id, workflow_id, body.definition)
    except plans.WorkflowValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.problems
        )
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found"
        )
    return workflow


@router.post("/{workflow_id}/publish", response_model=Workflow)
async def publish_workflow_route(
    workflow_id: str,
    merchant_id: str = Depends(
        merchant_scope("publish a workflow", "crm.workflows.publish")
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> Workflow:
    try:
        return await plans.publish_workflow(
            merchant_id, workflow_id, current_user.email
        )
    except plans.WorkflowNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found"
        )
    except plans.WorkflowValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.problems
        )


@router.post("/{workflow_id}/status", response_model=Workflow)
async def set_workflow_status_route(
    workflow_id: str,
    body: WorkflowStatusChange,
    merchant_id: str = Depends(
        merchant_scope("change a workflow's status", "crm.workflows.status")
    ),
) -> Workflow:
    try:
        workflow = await plans.set_status(merchant_id, workflow_id, body.status)
    except plans.WorkflowValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.problems
        )
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow not found (or archived)",
        )
    return workflow


@router.get("/{workflow_id}/runs", response_model=RunPage)
async def list_runs_route(
    workflow_id: str,
    run_status: Optional[Literal["waiting", "parked", "exited"]] = Query(
        None, alias="status", description="parked = the triage view"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    node: Optional[str] = Query(None, description="The square the run stands on"),
    version: Optional[int] = Query(None, ge=1, description="The version it runs"),
    exit_reason: Optional[str] = Query(None, description="How it ended"),
    q: Optional[str] = Query(
        None,
        max_length=120,
        description="Enrollment key, customer name or phone digits",
    ),
    since: Optional[datetime] = Query(None, description="Window start on entered_at"),
    until: Optional[datetime] = Query(None, description="Window end on entered_at"),
    anchor_entered_at: Optional[datetime] = Query(
        None,
        description="With anchor_id: echo page 1's `anchor` so later pages "
        "read at or before it and new runs cannot shift them",
    ),
    anchor_id: Optional[UUID] = Query(None, description="The anchor row's id"),
    merchant_id: str = Depends(
        merchant_scope("list workflow runs", "crm.workflows.runs")
    ),
) -> RunPage:
    """A plan's runs, newest first, as a page: the rows (each saying which
    of its key's runs it is, "Run 3 of 3"), the total matching the
    filters, and the anchor. Page 1 comes back with the anchor the server
    took from its newest row; pages 2+ echo it as the two anchor params,
    and read at or before it (a keyset on the sort's own fields), so a
    plan that keeps taking runs cannot shift the pages under the reader."""
    if (anchor_entered_at is None) != (anchor_id is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="anchor_entered_at and anchor_id go together",
        )
    rows, total = await runs.list_runs(
        merchant_id,
        workflow_id,
        run_status,
        limit,
        offset,
        node,
        version,
        exit_reason,
        q,
        since,
        until,
        (
            (anchor_entered_at, str(anchor_id))
            if anchor_entered_at is not None and anchor_id is not None
            else None
        ),
    )
    if anchor_entered_at is not None and anchor_id is not None:
        anchor: Optional[RunAnchor] = RunAnchor(
            entered_at=anchor_entered_at, id=anchor_id
        )
    elif rows:
        anchor = RunAnchor(entered_at=rows[0].entered_at, id=rows[0].id)
    else:
        anchor = None
    return RunPage(items=rows, total=total, anchor=anchor)


@router.get("/{workflow_id}/runs/{run_id}/calls", response_model=List[RunCall])
async def run_calls_route(
    workflow_id: str,
    run_id: str,
    merchant_id: str = Depends(
        merchant_scope("read a run's calls", "crm.workflows.runs")
    ),
) -> List[RunCall]:
    """Every call the run placed, in the order they were queued."""
    calls = await runs.run_calls(merchant_id, workflow_id, run_id)
    if calls is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run not found, or not this merchant's",
        )
    return calls


@router.get("/{workflow_id}/report", response_model=WorkflowReport)
async def workflow_report_route(
    workflow_id: str,
    since: Optional[datetime] = Query(
        None, description="Window start on entered_at (default: 30 days before until)"
    ),
    until: Optional[datetime] = Query(
        None, description="Window end on entered_at (default: now); at most 92 days"
    ),
    merchant_id: str = Depends(
        merchant_scope("read a workflow report", "crm.workflows.summary")
    ),
) -> WorkflowReport:
    """The day report for the runs that entered in the window: the
    customer table (how each journey stands, and whether we spoke to them
    before it ended) and the call table (what the dialler did)."""
    try:
        report = await analytics.workflow_report(merchant_id, workflow_id, since, until)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        )
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow not found, or not this merchant's",
        )
    return report


@router.get("/{workflow_id}/calls/summary", response_model=WorkflowCallSummary)
async def workflow_call_summary_route(
    workflow_id: str,
    since: Optional[datetime] = Query(
        None, description="Window start on entered_at (default: 30 days before until)"
    ),
    until: Optional[datetime] = Query(
        None, description="Window end on entered_at (default: now); at most 92 days"
    ),
    merchant_id: str = Depends(
        merchant_scope("read a workflow's calls", "crm.workflows.summary")
    ),
) -> WorkflowCallSummary:
    """The calls placed by the plan's runs that entered in the window."""
    try:
        return await analytics.workflow_call_summary(
            merchant_id, workflow_id, since, until
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        )


@router.get("/{workflow_id}/runs/{run_id}/steps", response_model=List[RunStep])
async def run_steps_route(
    workflow_id: str,
    run_id: str,
    limit: int = Query(200, ge=1, le=1000),
    merchant_id: str = Depends(
        merchant_scope("read a run's steps", "crm.workflows.runs")
    ),
) -> List[RunStep]:
    """Where this run has BEEN (canon T26): the closed squares, oldest
    first, with the square it stands on now appended — ``left_at`` null is
    what says "still here"."""
    steps = await runs.run_steps(merchant_id, workflow_id, run_id, limit)
    if steps is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run not found, or not this merchant's",
        )
    return steps


@router.post("/{workflow_id}/runs/{run_id}/resume", response_model=EnrollmentRun)
async def resume_run_route(
    workflow_id: str,
    run_id: str,
    merchant_id: str = Depends(
        merchant_scope("resume a parked run", "crm.workflows.resume")
    ),
) -> EnrollmentRun:
    run = await runs.resume_run(merchant_id, workflow_id, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run not found, not parked, or not this merchant's",
        )
    return run


@router.get("/{workflow_id}/summary", response_model=WorkflowRunSummary)
async def workflow_summary_route(
    workflow_id: str,
    since: Optional[datetime] = Query(None, description="Window start on entered_at"),
    until: Optional[datetime] = Query(None, description="Window end on entered_at"),
    tz: str = Query("Asia/Kolkata", description="Timezone of runs_per_day's days"),
    merchant_id: str = Depends(
        merchant_scope("read a workflow summary", "crm.workflows.summary")
    ),
) -> WorkflowRunSummary:
    return await analytics.workflow_summary(
        merchant_id, workflow_id, since, until, _timezone(tz)
    )


def _timezone(tz: str) -> str:
    """A tz the database will accept, or a 422 naming it — never a 500."""
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown timezone: {tz}",
        )
    return tz


@router.post("/{workflow_id}/validate", response_model=PublishCheck)
async def check_draft_route(
    workflow_id: str,
    merchant_id: str = Depends(
        merchant_scope("check a workflow draft", "crm.workflows.draft")
    ),
) -> PublishCheck:
    """What Publish would refuse on the saved draft, without publishing."""
    checked = await plans.check_draft(merchant_id, workflow_id)
    if checked is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found"
        )
    has_draft, problems = checked
    return PublishCheck(has_draft=has_draft, problems=problems)


@router.get("/{workflow_id}/versions", response_model=List[WorkflowVersion])
async def list_versions_route(
    workflow_id: str,
    merchant_id: str = Depends(
        merchant_scope("list workflow versions", "crm.workflows.versions")
    ),
) -> List[WorkflowVersion]:
    """Every published version, newest first, with the open runs still
    executing it (ADR 0023)."""
    return await versions.list_versions(merchant_id, workflow_id)


@router.post(
    "/{workflow_id}/versions/{from_version}/migrate", response_model=VersionMigration
)
async def migrate_version_route(
    workflow_id: str,
    from_version: int,
    to: int = Query(..., ge=1, description="The version those runs will execute"),
    merchant_id: str = Depends(
        merchant_scope("migrate runs to a version", "crm.workflows.migrate")
    ),
) -> VersionMigration:
    """Move every open run pinned to from_version under `to` — how a fix
    reaches runs in flight on a pin-mode plan. Refused (422) when `to`
    drops a square they stand on or changes the entry."""
    try:
        moved = await versions.migrate_forward(
            merchant_id, workflow_id, from_version, to
        )
    except versions.VersionNotFound as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{e} not found for this workflow",
        )
    except plans.WorkflowValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.problems
        )
    return VersionMigration(from_version=from_version, to_version=to, moved=moved)


@customer_router.get("/{customer_id}/runs", response_model=List[CustomerRun])
async def customer_runs_route(
    customer_id: str,
    limit: int = Query(100, ge=1, le=500),
    merchant_id: str = Depends(
        merchant_scope("read a customer's runs", "crm.workflows.journey")
    ),
) -> List[CustomerRun]:
    return await runs.customer_runs(merchant_id, customer_id, limit)

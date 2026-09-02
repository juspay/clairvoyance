"""/crm/workflows — the admin surface for plans (W1, ADR 0007: phase 1 is
ops/admin only; the merchant-facing builder is a console fast-follow).

Thin routes per module rules §1: auth via Depends, delegate to plans.py.
Tenancy law: merchant_id is a required query param on every route.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.logger.context import set_log_context
from app.crm.auth import crm_admin_user
from app.crm.outreach import plans, runs
from app.crm.outreach.schemas import (
    CustomerRun,
    EnrollmentRun,
    Workflow,
    WorkflowRunSummary,
    WorkflowSummary,
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
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(crm_admin_user),
) -> Workflow:
    set_log_context(component="crm.workflows.create", merchant_id=merchant_id)
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
    merchant_id: str = Query(..., description="Tenant scope — required"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: UserInfo = Depends(crm_admin_user),
) -> List[WorkflowSummary]:
    set_log_context(component="crm.workflows.list", merchant_id=merchant_id)
    return await plans.list_workflows(merchant_id, limit, offset)


@router.get("/{workflow_id}", response_model=Workflow)
async def get_workflow_route(
    workflow_id: str,
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(crm_admin_user),
) -> Workflow:
    set_log_context(component="crm.workflows.get", merchant_id=merchant_id)
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
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(crm_admin_user),
) -> Workflow:
    set_log_context(component="crm.workflows.draft", merchant_id=merchant_id)
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
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(crm_admin_user),
) -> Workflow:
    set_log_context(component="crm.workflows.publish", merchant_id=merchant_id)
    try:
        return await plans.publish_workflow(merchant_id, workflow_id)
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
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(crm_admin_user),
) -> Workflow:
    set_log_context(component="crm.workflows.status", merchant_id=merchant_id)
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


@router.get("/{workflow_id}/runs", response_model=List[EnrollmentRun])
async def list_runs_route(
    workflow_id: str,
    merchant_id: str = Query(..., description="Tenant scope — required"),
    run_status: Optional[Literal["waiting", "parked", "exited"]] = Query(
        None, alias="status", description="parked = the triage view"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: UserInfo = Depends(crm_admin_user),
) -> List[EnrollmentRun]:
    set_log_context(component="crm.workflows.runs", merchant_id=merchant_id)
    return await runs.list_runs(merchant_id, workflow_id, run_status, limit, offset)


@router.post("/{workflow_id}/runs/{run_id}/resume", response_model=EnrollmentRun)
async def resume_run_route(
    workflow_id: str,
    run_id: str,
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(crm_admin_user),
) -> EnrollmentRun:
    set_log_context(component="crm.workflows.resume", merchant_id=merchant_id)
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
    merchant_id: str = Query(..., description="Tenant scope — required"),
    since: Optional[datetime] = Query(None, description="Window start on entered_at"),
    until: Optional[datetime] = Query(None, description="Window end on entered_at"),
    current_user: UserInfo = Depends(crm_admin_user),
) -> WorkflowRunSummary:
    set_log_context(component="crm.workflows.summary", merchant_id=merchant_id)
    return await runs.workflow_summary(merchant_id, workflow_id, since, until)


@customer_router.get("/{customer_id}/runs", response_model=List[CustomerRun])
async def customer_runs_route(
    customer_id: str,
    merchant_id: str = Query(..., description="Tenant scope — required"),
    limit: int = Query(100, ge=1, le=500),
    current_user: UserInfo = Depends(crm_admin_user),
) -> List[CustomerRun]:
    set_log_context(component="crm.workflows.journey", merchant_id=merchant_id)
    return await runs.customer_runs(merchant_id, customer_id, limit)

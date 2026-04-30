"""FastAPI route handlers for the template test API."""

import uuid
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, HTTPException, status

from app.ai.voice.agents.breeze_buddy.test.runner import (
    run_scenarios_llm,
    run_scenarios_structural,
)
from app.ai.voice.agents.breeze_buddy.test.scenario_generator import generate_scenarios
from app.ai.voice.agents.breeze_buddy.test.types import (
    GeneratedScenario,
    GenerateScenariosJobPollResponse,
    GenerateScenariosJobResponse,
    GenerateScenariosRequest,
    GenerationTier,
    JobStatus,
    ScenarioRunResult,
    TestMode,
    TestRunPollResponse,
    TestRunRequest,
    TestRunResponse,
)
from app.api.routers.breeze_buddy.templates.rbac import validate_template_access
from app.core.logger import logger
from app.database.accessor.breeze_buddy.template import get_template_by_id
from app.schemas import UserInfo

# ---------------------------------------------------------------------------
# In-memory job stores (ephemeral — no DB persistence needed)
# ---------------------------------------------------------------------------

_run_store: Dict[str, "_RunState"] = {}
_generate_store: Dict[str, "_GenerateState"] = {}


class _RunState:
    def __init__(
        self, run_id: str, template_id: str, mode: TestMode, total: int
    ) -> None:
        self.run_id = run_id
        self.template_id = template_id
        self.mode = mode
        self.status = JobStatus.RUNNING
        self.completed = 0
        self.total = total
        self.results: List[ScenarioRunResult] = []
        self.error: Optional[str] = None


class _GenerateState:
    def __init__(self, job_id: str, template_id: str) -> None:
        self.job_id = job_id
        self.template_id = template_id
        self.status = JobStatus.RUNNING
        self.template_name: Optional[str] = None
        self.scenarios: Optional[List[GeneratedScenario]] = None
        self.error: Optional[str] = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _fetch_template_or_404(template_id: str, current_user: UserInfo):
    template = await get_template_by_id(template_id)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}",
        )
    validate_template_access(
        current_user,
        template.reseller_id,
        template.merchant_id,
        operation="test template",
    )
    return template


def _build_summary(results: List[ScenarioRunResult]) -> Dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    latencies = [r.total_latency_ms for r in results if r.total_latency_ms is not None]
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total * 100, 1) if total else 0.0,
        "avg_latency_ms": (
            round(sum(latencies) / len(latencies), 1) if latencies else None
        ),
    }


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------


async def _generate_scenarios_background(
    job_id: str,
    template_id: str,
    payload_example: Dict[str, Any],
    tier: GenerationTier,
) -> None:
    state = _generate_store.get(job_id)
    if not state:
        return
    try:
        template = await get_template_by_id(template_id)
        if not template:
            state.status = JobStatus.FAILED
            state.error = f"Template '{template_id}' not found"
            return
        state.template_name = template.name
        state.scenarios = await generate_scenarios(
            template=template,
            payload_example=payload_example or None,
            tier=tier,
        )
        state.status = JobStatus.COMPLETED
    except Exception as exc:
        logger.error("Generate job {} failed: {}", job_id, repr(exc), exc_info=True)
        state.status = JobStatus.FAILED
        state.error = str(exc)


async def _run_llm_background(
    run_id: str, template_id: str, scenarios: List[GeneratedScenario]
) -> None:
    state = _run_store.get(run_id)
    if not state:
        return
    # Capture as non-optional so pyrefly can narrow it inside the closure below.
    _state: _RunState = state
    try:
        template = await get_template_by_id(template_id)
        if not template:
            _state.status = JobStatus.FAILED
            _state.error = f"Template '{template_id}' not found"
            return

        async def on_progress(
            completed: int, total: int, result: ScenarioRunResult
        ) -> None:
            _state.results.append(result)
            _state.completed = completed

        await run_scenarios_llm(template, scenarios, on_progress=on_progress)
        _state.status = JobStatus.COMPLETED
    except Exception as exc:
        logger.error("LLM run {} failed: {}", run_id, repr(exc), exc_info=True)
        _state.status = JobStatus.FAILED
        _state.error = str(exc)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def generate_scenarios_handler(
    template_id: str,
    body: GenerateScenariosRequest,
    background_tasks: BackgroundTasks,
    current_user: UserInfo,
) -> GenerateScenariosJobResponse:
    """Queue LLM scenario generation and return a job_id immediately."""
    logger.info(
        "User {} queuing {} scenario generation for template {}",
        current_user.username,
        body.tier.value,
        template_id,
    )
    await _fetch_template_or_404(template_id, current_user)

    job_id = str(uuid.uuid4())
    _generate_store[job_id] = _GenerateState(job_id=job_id, template_id=template_id)
    background_tasks.add_task(
        _generate_scenarios_background,
        job_id,
        template_id,
        body.payload_example or {},
        body.tier,
    )
    return GenerateScenariosJobResponse(
        job_id=job_id, template_id=template_id, status=JobStatus.RUNNING
    )


async def poll_generate_handler(
    template_id: str,
    job_id: str,
    current_user: UserInfo,
) -> GenerateScenariosJobPollResponse:
    """Poll the status of a background scenario generation job."""
    state = _generate_store.get(job_id)
    if not state or state.template_id != template_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Generate job not found: {job_id}",
        )
    return GenerateScenariosJobPollResponse(
        job_id=job_id,
        template_id=template_id,
        status=state.status,
        template_name=state.template_name,
        scenarios=state.scenarios,
        error=state.error,
    )


async def start_test_run_handler(
    template_id: str,
    body: TestRunRequest,
    background_tasks: BackgroundTasks,
    current_user: UserInfo,
) -> TestRunResponse:
    """Start a structural (sync) or LLM (async) test run."""
    logger.info(
        "User {} starting {} run for template {} ({} scenarios)",
        current_user.username,
        body.mode,
        template_id,
        len(body.scenarios),
    )
    template = await _fetch_template_or_404(template_id, current_user)
    run_id = str(uuid.uuid4())

    if body.mode == TestMode.STRUCTURAL:
        try:
            results = await run_scenarios_structural(template, body.scenarios)
        except Exception as exc:
            logger.error(
                "Structural run failed for {}: {}",
                template_id,
                repr(exc),
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
            )
        return TestRunResponse(
            run_id=run_id,
            template_id=template_id,
            status=JobStatus.COMPLETED,
            mode=body.mode,
            results=results,
            total_scenarios=len(results),
        )

    # LLM mode — async
    state = _RunState(
        run_id=run_id,
        template_id=template_id,
        mode=body.mode,
        total=len(body.scenarios),
    )
    _run_store[run_id] = state
    background_tasks.add_task(_run_llm_background, run_id, template_id, body.scenarios)
    return TestRunResponse(
        run_id=run_id,
        template_id=template_id,
        status=JobStatus.RUNNING,
        mode=body.mode,
        total_scenarios=len(body.scenarios),
    )


async def poll_test_run_handler(
    template_id: str,
    run_id: str,
    current_user: UserInfo,
) -> TestRunPollResponse:
    """Poll the progress of an LLM-mode test run."""
    state = _run_store.get(run_id)
    if not state or state.template_id != template_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Test run not found: {run_id}",
        )
    return TestRunPollResponse(
        run_id=run_id,
        template_id=template_id,
        status=state.status,
        mode=state.mode,
        progress={"completed": state.completed, "total": state.total},
        results=list(state.results),
        summary=(
            _build_summary(state.results)
            if state.status == JobStatus.COMPLETED
            else None
        ),
    )

"""
Template testing API endpoints.

Endpoints:
  POST /templates/{template_id}/test/generate          — Queue LLM scenario generation (async)
  GET  /templates/{template_id}/test/generate/{job_id} — Poll generation job status
  POST /templates/{template_id}/test/run               — Run scenarios (structural or LLM)
  GET  /templates/{template_id}/test/run/{run_id}      — Poll LLM run progress
"""

from fastapi import APIRouter, BackgroundTasks, Depends

from app.ai.voice.agents.breeze_buddy.test.types import (
    GenerateScenariosJobPollResponse,
    GenerateScenariosJobResponse,
    GenerateScenariosRequest,
    TestRunPollResponse,
    TestRunRequest,
    TestRunResponse,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo

from .handlers import (
    generate_scenarios_handler,
    poll_generate_handler,
    poll_test_run_handler,
    start_test_run_handler,
)

router = APIRouter()


@router.post(
    "/templates/{template_id}/test/generate",
    response_model=GenerateScenariosJobResponse,
    summary="Queue LLM scenario generation for a template",
    tags=["template-testing"],
)
async def generate_test_scenarios(
    template_id: str,
    body: GenerateScenariosRequest,
    background_tasks: BackgroundTasks,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Queues an LLM-based scenario generation job and returns a `job_id`
    immediately (no timeout risk).

    Poll `GET /templates/{template_id}/test/generate/{job_id}` every 2–3 seconds until
    `status == "completed"` or `"failed"`.
    """
    return await generate_scenarios_handler(
        template_id, body, background_tasks, current_user
    )


@router.get(
    "/templates/{template_id}/test/generate/{job_id}",
    response_model=GenerateScenariosJobPollResponse,
    summary="Poll the status of a scenario generation job",
    tags=["template-testing"],
)
async def poll_generate_scenarios(
    template_id: str,
    job_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Poll the progress of a background scenario generation job.

    Returns `status`, and when `status == "completed"`, the generated
    `scenarios` list and `template_name`.
    """
    return await poll_generate_handler(template_id, job_id, current_user)


@router.post(
    "/templates/{template_id}/test/run",
    response_model=TestRunResponse,
    summary="Run test scenarios against a template",
    tags=["template-testing"],
)
async def run_test_scenarios(
    template_id: str,
    body: TestRunRequest,
    background_tasks: BackgroundTasks,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Run the provided scenarios against the template.

    **Structural mode** (fast, ~10 ms/scenario): validates node and function
    existence without making LLM calls. Results returned synchronously.

    **LLM mode** (slow, ~2–10 s/scenario): sends actual messages to Azure
    OpenAI and asserts on function calls and node transitions. Run starts
    asynchronously — poll `GET /templates/{template_id}/test/run/{run_id}` for progress.
    """
    return await start_test_run_handler(
        template_id, body, background_tasks, current_user
    )


@router.get(
    "/templates/{template_id}/test/run/{run_id}",
    response_model=TestRunPollResponse,
    summary="Poll the status of an LLM test run",
    tags=["template-testing"],
)
async def poll_test_run(
    template_id: str,
    run_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Poll the progress of a running LLM-mode test.

    Returns current status, number of completed scenarios, and any
    results completed so far.  When `status == "completed"`, all results
    and a summary are included.
    """
    return await poll_test_run_handler(template_id, run_id, current_user)

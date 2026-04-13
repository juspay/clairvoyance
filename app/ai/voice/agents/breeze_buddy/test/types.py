"""Pydantic models for the Breeze Buddy template test API."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TestMode(str, Enum):
    STRUCTURAL = "structural"
    LLM = "llm"


class JobStatus(str, Enum):
    """Shared status enum for both generate jobs and run jobs."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# Aliases kept for backward-compat with existing handler/router imports
RunStatus = JobStatus
GenerateJobStatus = JobStatus


class GenerationTier(str, Enum):
    """Controls how many scenario variants are generated per flow path.

    BASIC   — 1 scenario per path  (fast, minimal)
    ADVANCED — 2-3 scenarios per path (moderate depth, different user styles)
    PRO     — 4-5 scenarios per path  (thorough, diverse personas & edge cases)
    """

    BASIC = "basic"
    ADVANCED = "advanced"
    PRO = "pro"


# ---------------------------------------------------------------------------
# Turn models
# ---------------------------------------------------------------------------


class FunctionFailureConfig(BaseModel):
    """Inject a mock error response for a named function on a given turn."""

    function_name: str
    error_response: Dict[str, Any] = Field(
        default_factory=lambda: {"status": "error", "message": "Simulated failure"}
    )


class TurnInput(BaseModel):
    """One user turn in a test scenario."""

    user_message: str
    expect_function_call: Optional[str] = None
    expect_no_function_call: bool = False
    expect_node: Optional[str] = None
    simulate_function_failures: List[FunctionFailureConfig] = Field(
        default_factory=list
    )


class TurnResult(BaseModel):
    """Outcome of a single executed turn."""

    turn_index: int
    user_message: str
    bot_response: str
    function_called: Optional[str] = None
    function_args: Optional[Dict[str, Any]] = None
    # 'ok' | 'simulated_failure' | 'not_called'
    function_execution_status: str = "ok"
    function_error_injected: Optional[Dict[str, Any]] = None
    node_name: Optional[str] = None
    passed: bool
    failure_reason: Optional[str] = None
    latency_ms: Optional[float] = None


# ---------------------------------------------------------------------------
# Scenario models
# ---------------------------------------------------------------------------


class GeneratedScenario(BaseModel):
    """A test scenario — LLM-generated or user-authored."""

    id: str
    name: str
    scenario_type: str = "other"
    description: Optional[str] = None
    payload_example: Dict[str, Any] = Field(default_factory=dict)
    turns: List[TurnInput] = Field(..., min_length=1)
    expected_outcome: Optional[str] = None
    expected_final_node: Optional[str] = None


class ScenarioRunResult(BaseModel):
    """Result of running a single scenario."""

    scenario_id: str
    scenario_name: str
    scenario_type: str
    passed: bool
    failure_reason: Optional[str] = None
    turns: List[TurnResult] = Field(default_factory=list)
    nodes_visited: List[str] = Field(default_factory=list)
    final_node: Optional[str] = None
    actual_outcome: Optional[str] = None
    total_latency_ms: Optional[float] = None
    simulated_failures: List[Dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Generate-scenarios API models
# ---------------------------------------------------------------------------


class GenerateScenariosRequest(BaseModel):
    """POST /templates/{id}/test/generate"""

    payload_example: Dict[str, Any] = Field(default_factory=dict)
    tier: GenerationTier = Field(default=GenerationTier.BASIC)


class GenerateScenariosJobResponse(BaseModel):
    """Immediate response — poll GET /test/generate/{job_id} for result."""

    job_id: str
    template_id: str
    status: JobStatus = JobStatus.RUNNING


class GenerateScenariosJobPollResponse(BaseModel):
    """GET /templates/{id}/test/generate/{job_id}"""

    job_id: str
    template_id: str
    status: JobStatus
    template_name: Optional[str] = None
    scenarios: Optional[List[GeneratedScenario]] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Run API models
# ---------------------------------------------------------------------------


class TestRunRequest(BaseModel):
    """POST /templates/{id}/test/run"""

    scenarios: List[GeneratedScenario] = Field(..., min_length=1)
    mode: TestMode = TestMode.STRUCTURAL


class TestRunResponse(BaseModel):
    """POST /templates/{id}/test/run response."""

    run_id: str
    template_id: str
    status: JobStatus
    mode: TestMode
    results: Optional[List[ScenarioRunResult]] = None
    total_scenarios: Optional[int] = None


class TestRunPollResponse(BaseModel):
    """GET /templates/{id}/test/run/{run_id}"""

    run_id: str
    template_id: str
    status: JobStatus
    mode: TestMode
    progress: Dict[str, int] = Field(default_factory=dict)
    results: List[ScenarioRunResult] = Field(default_factory=list)
    summary: Optional[Dict[str, Any]] = None

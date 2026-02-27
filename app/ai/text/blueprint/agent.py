"""
Blueprint Agent — Deep Agent orchestrator for template generation.

Takes a natural language use-case description and produces a production-ready
Clairvoyance template JSON through a 3-agent pipeline:

  1. Template Architect (Claude Sonnet) — Generates the structural JSON
  2. Dialogue Enhancer (GPT-4o) — Polishes all human-facing dialogue
  3. Reviewer (Claude Sonnet) — Validates against the Pydantic schema

Uses LangChain Deep Agents for orchestration, context isolation,
and dynamic codebase awareness.
"""

import time
from enum import Enum
from typing import Any, AsyncGenerator, Dict, Optional

from pydantic import BaseModel

from app.ai.text.blueprint.prompts import (
    ARCHITECT_SYSTEM_PROMPT,
    ENHANCER_SYSTEM_PROMPT,
    REVIEWER_SYSTEM_PROMPT,
)
from app.core.logger import logger


def _import_deep_agents():
    """Lazy import for deepagents and langchain (optional dependency).

    Install with: uv sync --extra text-agents
    """
    try:
        from deepagents import create_deep_agent  # type: ignore[import-not-found]
        from langchain.chat_models import (  # type: ignore[import-not-found]
            init_chat_model,
        )

        return init_chat_model, create_deep_agent
    except ImportError as e:
        raise ImportError(
            "deepagents and langchain are required for the Blueprint agent. "
            "Install with: uv sync --extra text-agents"
        ) from e


def _import_template_tools():
    """Lazy import for template awareness tools.

    Returns the tools list or an empty list if langchain_core is unavailable.
    """
    try:
        from app.ai.text.blueprint.tools.template_tools import (
            get_template_by_id_tool,
            list_templates_tool,
        )

        return [list_templates_tool, get_template_by_id_tool]
    except ImportError:
        logger.warning("Template tools unavailable — langchain_core not installed")
        return []


# ---------------------------------------------------------------------------
# Pipeline status tracking
# ---------------------------------------------------------------------------


class PipelineStage(str, Enum):
    """Stages in the Blueprint generation pipeline."""

    INITIALIZING = "initializing"
    PLANNING = "planning"
    ARCHITECT_RUNNING = "architect_running"
    ARCHITECT_COMPLETE = "architect_complete"
    ENHANCER_RUNNING = "enhancer_running"
    ENHANCER_COMPLETE = "enhancer_complete"
    REVIEWER_RUNNING = "reviewer_running"
    REVIEWER_COMPLETE = "reviewer_complete"
    RETRY_ARCHITECT = "retry_architect"
    COMPLETED = "completed"
    ERROR = "error"


class PipelineStatus(BaseModel):
    """Real-time status update for the pipeline."""

    stage: PipelineStage
    message: str
    progress_pct: int = 0
    agent_name: Optional[str] = None
    elapsed_secs: float = 0.0
    detail: Optional[str] = None


# Mapping subagent names to pipeline stages
_AGENT_STAGE_MAP = {
    "template-architect": (
        PipelineStage.ARCHITECT_RUNNING,
        PipelineStage.ARCHITECT_COMPLETE,
    ),
    "dialogue-enhancer": (
        PipelineStage.ENHANCER_RUNNING,
        PipelineStage.ENHANCER_COMPLETE,
    ),
    "reviewer": (
        PipelineStage.REVIEWER_RUNNING,
        PipelineStage.REVIEWER_COMPLETE,
    ),
}

# Progress percentages for each stage
_STAGE_PROGRESS = {
    PipelineStage.INITIALIZING: 0,
    PipelineStage.PLANNING: 5,
    PipelineStage.ARCHITECT_RUNNING: 15,
    PipelineStage.ARCHITECT_COMPLETE: 40,
    PipelineStage.ENHANCER_RUNNING: 45,
    PipelineStage.ENHANCER_COMPLETE: 70,
    PipelineStage.REVIEWER_RUNNING: 75,
    PipelineStage.REVIEWER_COMPLETE: 95,
    PipelineStage.RETRY_ARCHITECT: 50,
    PipelineStage.COMPLETED: 100,
    PipelineStage.ERROR: -1,
}


def _make_status(
    stage: PipelineStage,
    message: str,
    start_time: float,
    agent_name: Optional[str] = None,
    detail: Optional[str] = None,
) -> PipelineStatus:
    """Create a PipelineStatus with elapsed time calculated."""
    return PipelineStatus(
        stage=stage,
        message=message,
        progress_pct=_STAGE_PROGRESS.get(stage, 0),
        agent_name=agent_name,
        elapsed_secs=round(time.time() - start_time, 1),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Subagent definitions
# ---------------------------------------------------------------------------

TEMPLATE_ARCHITECT = {
    "name": "template-architect",
    "description": (
        "Generates a complete Clairvoyance voice agent template JSON from a "
        "natural language description. Dynamically reads the codebase for "
        "current schema awareness (types.py, example templates, hooks). "
        "Has tools to list and fetch production templates from the database."
    ),
    "system_prompt": ARCHITECT_SYSTEM_PROMPT,
    "tools": [],  # Populated at runtime by create_blueprint_agent
    "model": "anthropic:claude-sonnet-4-20250514",
}

DIALOGUE_ENHANCER = {
    "name": "dialogue-enhancer",
    "description": (
        "Enhances all dialogue content in a template JSON to sound natural, "
        "warm, and human-like. Only modifies text content (role_messages, "
        "task_messages, function descriptions) — never touches structure."
    ),
    "system_prompt": ENHANCER_SYSTEM_PROMPT,
    "tools": [],
    "model": "openai:gpt-4o",
}

REVIEWER = {
    "name": "reviewer",
    "description": (
        "Validates a template JSON against the Pydantic TemplateModel schema, "
        "checks flow integrity (no dead ends, valid transitions, correct hooks), "
        "and ensures production readiness. Fixes issues when possible."
    ),
    "system_prompt": REVIEWER_SYSTEM_PROMPT,
    "tools": [],
    "model": "anthropic:claude-sonnet-4-20250514",
}

# ---------------------------------------------------------------------------
# Orchestrator system prompt
# ---------------------------------------------------------------------------

ORCHESTRATOR_SYSTEM_PROMPT = """\
You are the Blueprint Orchestrator for the Clairvoyance voice agent platform.

Your job is to coordinate template generation by delegating to specialized
subagents in sequence. You manage the pipeline and ensure quality.

## Pipeline

When a user describes a voice agent use case:

1. **Plan** — Use write_todos to outline the template generation steps.

2. **Generate Structure** — Delegate to `template-architect` with the user's
   description. The architect will read the codebase, study example templates,
   and produce a structurally complete template JSON. It writes the draft
   to a file.

3. **Enhance Dialogue** — Delegate to `dialogue-enhancer` with instructions
   to read the draft template file and enhance all dialogue content. It writes
   the enhanced version back to a file.

4. **Validate** — Delegate to `reviewer` with instructions to read the
   enhanced template file and validate it against the schema. The reviewer
   either confirms the template is production-ready or fixes issues.

5. **Handle Failures** — If the reviewer finds CRITICAL issues that it
   could not auto-fix, send the feedback back to `template-architect`
   for correction, then re-run the enhancer and reviewer. Maximum 2 retry
   cycles.

6. **Return Result** — Read the final validated template file and return
   the complete JSON to the user.

## File Convention

Use these file paths for inter-agent communication:
- Draft template: `blueprint_draft.json`
- Enhanced template: `blueprint_enhanced.json`
- Final template: `blueprint_final.json`

## Template Reference Awareness

The template-architect has access to two database tools:
- `list_templates_tool`: Returns metadata (name, ID, merchant) for all
  production templates. Use this to discover existing templates.
- `get_template_by_id_tool`: Fetches a full template JSON by UUID. Use this
  when the user references an existing template by name (e.g. "like the
  order confirmation template" or "based on auto-confirm").

When the user references an existing template:
1. Tell the architect to call `list_templates_tool` first to find matching
   templates by name.
2. Then call `get_template_by_id_tool` with the closest match's ID.
3. Use the fetched template as structural reference for the new generation.

## Important Rules

- Always start by planning with write_todos.
- Pass the user's FULL description to the architect — do not summarize.
- If the user provides specific requirements (merchant name, voice, language,
  specific nodes, payload fields), include ALL of them in the delegation.
- If the user references an existing template by name, instruct the architect
  to use list_templates_tool and get_template_by_id_tool to fetch it.
- After the pipeline completes, read blueprint_final.json and present the
  complete JSON to the user.
- If the user asks for modifications to an existing template, read the
  template, apply changes, and re-run the enhancer + reviewer pipeline.
"""

# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------


def create_blueprint_agent(
    orchestrator_model: str = "anthropic:claude-sonnet-4-20250514",
    architect_model: Optional[str] = None,
    enhancer_model: Optional[str] = None,
    reviewer_model: Optional[str] = None,
) -> Any:
    """Create and return the Blueprint deep agent.

    Args:
        orchestrator_model: Model for the main orchestrator.
        architect_model: Override model for the Template Architect.
        enhancer_model: Override model for the Dialogue Enhancer.
        reviewer_model: Override model for the Reviewer.

    Returns:
        A compiled LangGraph graph (deep agent) ready for invocation.
    """
    architect = {**TEMPLATE_ARCHITECT}
    enhancer = {**DIALOGUE_ENHANCER}
    reviewer = {**REVIEWER}

    # Inject template awareness tools into the architect
    architect["tools"] = _import_template_tools()

    if architect_model:
        architect["model"] = architect_model
    if enhancer_model:
        enhancer["model"] = enhancer_model
    if reviewer_model:
        reviewer["model"] = reviewer_model

    logger.info(
        f"Creating Blueprint agent — orchestrator: {orchestrator_model}, "
        f"architect: {architect['model']}, enhancer: {enhancer['model']}, "
        f"reviewer: {reviewer['model']}"
    )

    init_chat_model, create_deep_agent = _import_deep_agents()

    agent = create_deep_agent(
        model=init_chat_model(orchestrator_model),
        system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
        subagents=[architect, enhancer, reviewer],
    )

    logger.info("Blueprint agent created successfully")
    return agent


# ---------------------------------------------------------------------------
# Non-streaming invocation
# ---------------------------------------------------------------------------


async def generate_template(
    description: str,
    orchestrator_model: str = "anthropic:claude-sonnet-4-20250514",
    architect_model: Optional[str] = None,
    enhancer_model: Optional[str] = None,
    reviewer_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a template from a natural language description.

    Args:
        description: Natural language description of the voice agent use case.
        orchestrator_model: Model for the main orchestrator.
        architect_model: Override model for the Template Architect.
        enhancer_model: Override model for the Dialogue Enhancer.
        reviewer_model: Override model for the Reviewer.

    Returns:
        Dict with status, message, and result.
    """
    agent = create_blueprint_agent(
        orchestrator_model=orchestrator_model,
        architect_model=architect_model,
        enhancer_model=enhancer_model,
        reviewer_model=reviewer_model,
    )

    logger.info(f"Generating template for: {description[:100]}...")

    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": description}]}
        )

        messages = result.get("messages", [])
        final_message = messages[-1]["content"] if messages else ""

        logger.info("Template generation completed successfully")

        return {
            "status": "success",
            "message": "Template generated and validated successfully",
            "result": final_message,
        }
    except Exception as e:
        logger.error(f"Template generation failed: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Template generation failed: {str(e)}",
            "result": None,
        }


# ---------------------------------------------------------------------------
# Streaming invocation (SSE-compatible)
# ---------------------------------------------------------------------------


async def generate_template_stream(
    description: str,
    orchestrator_model: str = "anthropic:claude-sonnet-4-20250514",
    architect_model: Optional[str] = None,
    enhancer_model: Optional[str] = None,
    reviewer_model: Optional[str] = None,
) -> AsyncGenerator[PipelineStatus, None]:
    """Generate a template with real-time status updates via async generator.

    Yields PipelineStatus objects that can be serialized to SSE events.
    The caller (API layer) converts these to text/event-stream format.

    Args:
        description: Natural language description of the voice agent use case.
        orchestrator_model: Model for the main orchestrator.
        architect_model: Override model for the Template Architect.
        enhancer_model: Override model for the Dialogue Enhancer.
        reviewer_model: Override model for the Reviewer.

    Yields:
        PipelineStatus objects representing each stage of the pipeline.
    """
    start_time = time.time()

    # Stage 1: Initializing
    yield _make_status(
        PipelineStage.INITIALIZING,
        "Creating Blueprint agent pipeline...",
        start_time,
    )

    try:
        agent = create_blueprint_agent(
            orchestrator_model=orchestrator_model,
            architect_model=architect_model,
            enhancer_model=enhancer_model,
            reviewer_model=reviewer_model,
        )
    except Exception as e:
        yield _make_status(
            PipelineStage.ERROR,
            f"Failed to create agent: {str(e)}",
            start_time,
        )
        return

    # Stage 2: Stream events from the deep agent
    yield _make_status(
        PipelineStage.PLANNING,
        "Planning template generation steps...",
        start_time,
    )

    PipelineStage.PLANNING
    last_agent: Optional[str] = None

    try:
        async for event in agent.astream_events(
            {"messages": [{"role": "user", "content": description}]},
            version="v2",
        ):
            event_kind = event.get("event", "")
            event_name = event.get("name", "")
            event.get("tags", [])

            # Detect subagent invocations via the "task" tool
            if event_kind == "on_tool_start" and event_name == "task":
                tool_input = event.get("data", {}).get("input", {})
                agent_name = _extract_agent_name(tool_input)
                if agent_name and agent_name in _AGENT_STAGE_MAP:
                    running_stage, _ = _AGENT_STAGE_MAP[agent_name]
                    last_agent = agent_name
                    yield _make_status(
                        running_stage,
                        f"{_agent_display_name(agent_name)} is working...",
                        start_time,
                        agent_name=agent_name,
                    )

            elif event_kind == "on_tool_end" and event_name == "task":
                if last_agent and last_agent in _AGENT_STAGE_MAP:
                    _, complete_stage = _AGENT_STAGE_MAP[last_agent]
                    yield _make_status(
                        complete_stage,
                        f"{_agent_display_name(last_agent)} finished.",
                        start_time,
                        agent_name=last_agent,
                    )
                    last_agent = None

            # Detect write_todos calls (planning)
            elif event_kind == "on_tool_start" and event_name == "write_todos":
                yield _make_status(
                    PipelineStage.PLANNING,
                    "Orchestrator is planning the pipeline steps...",
                    start_time,
                    agent_name="orchestrator",
                )

        # Final: completed
        yield _make_status(
            PipelineStage.COMPLETED,
            "Template generated and validated successfully.",
            start_time,
        )

    except Exception as e:
        logger.error(f"Stream generation failed: {e}", exc_info=True)
        yield _make_status(
            PipelineStage.ERROR,
            f"Pipeline error: {str(e)}",
            start_time,
        )


def _extract_agent_name(tool_input: Any) -> Optional[str]:
    """Extract the subagent name from a task tool invocation input."""
    if isinstance(tool_input, dict):
        # Deep agents pass subagent name in the input
        name = tool_input.get("name") or tool_input.get("agent_name")
        if name:
            return name
        # Fall back to checking the prompt for subagent references
        prompt = tool_input.get("prompt", "")
        for agent_key in _AGENT_STAGE_MAP:
            if agent_key in prompt.lower():
                return agent_key
    return None


def _agent_display_name(name: str) -> str:
    """Convert agent key to human-readable display name."""
    display_names = {
        "template-architect": "Template Architect (Claude)",
        "dialogue-enhancer": "Dialogue Enhancer (GPT-4o)",
        "reviewer": "Reviewer (Claude)",
    }
    return display_names.get(name, name)

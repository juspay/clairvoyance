"""
Text Agent Playground — Generic test harness for all text-based agents.

Run directly to interactively test any registered text agent:

    python -m app.ai.text.playground

Or use the FastAPI endpoint:

    POST /agent/text/playground/run
    {
        "agent": "blueprint",
        "input": "Create a voice agent for appointment reminders..."
    }

This module provides:
- CLI mode for local development and testing
- API endpoints for programmatic testing
- Registered agent discovery and invocation
- Real-time SSE output for monitoring agent progress

Lives under app/ai/text/ because it is generic to ALL text agents,
not specific to any single agent like Blueprint.
"""

import asyncio
import time
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from app.core.logger import logger

# ---------------------------------------------------------------------------
# Agent registry — register new text agents here
# ---------------------------------------------------------------------------

_AGENT_REGISTRY: Dict[str, Dict[str, Any]] = {}


def register_agent(
    name: str,
    description: str,
    invoke_fn: Any,
    stream_fn: Optional[Any] = None,
) -> None:
    """Register a text agent for playground access.

    Args:
        name: Unique agent identifier (e.g., "blueprint").
        description: Human-readable description of what the agent does.
        invoke_fn: Async callable(input: str) -> dict with status/result.
        stream_fn: Optional async generator(input: str) -> yields status objects.
    """
    _AGENT_REGISTRY[name] = {
        "name": name,
        "description": description,
        "invoke_fn": invoke_fn,
        "stream_fn": stream_fn,
    }
    logger.info(f"Registered text agent: {name}")


def list_agents() -> list:
    """Return list of registered agents with their descriptions."""
    return [
        {"name": info["name"], "description": info["description"]}
        for info in _AGENT_REGISTRY.values()
    ]


def get_agent(name: str) -> Optional[Dict[str, Any]]:
    """Get a registered agent by name."""
    return _AGENT_REGISTRY.get(name)


# ---------------------------------------------------------------------------
# Register Blueprint agent
# ---------------------------------------------------------------------------


def _register_defaults():
    """Register the default text agents.

    Imports are lazy to avoid crashing when optional dependencies
    (deepagents, langchain) are not installed.
    """
    try:
        from app.ai.text.blueprint.agent import (
            generate_template,
            generate_template_stream,
        )

        register_agent(
            name="blueprint",
            description=(
                "Generates production-ready Clairvoyance voice agent templates "
                "from natural language descriptions. Uses a 3-agent pipeline: "
                "Template Architect (Claude) -> Dialogue Enhancer (GPT-4o) -> "
                "Reviewer (Claude)."
            ),
            invoke_fn=generate_template,
            stream_fn=generate_template_stream,
        )
    except ImportError:
        logger.warning(
            "Blueprint agent not registered: deepagents/langchain not installed. "
            "Install with: uv sync --extra text-agents"
        )


# ---------------------------------------------------------------------------
# Request / Response models (for API usage)
# ---------------------------------------------------------------------------


class PlaygroundRequest(BaseModel):
    """Request body for playground invocation."""

    agent: str = Field(
        ...,
        description="Name of the registered text agent to invoke.",
        examples=["blueprint"],
    )
    input: str = Field(
        ...,
        min_length=5,
        description="The input prompt / description to send to the agent.",
    )


class PlaygroundResponse(BaseModel):
    """Response body for playground invocation."""

    agent: str
    status: str
    message: str
    result: Optional[str] = None
    elapsed_secs: float = 0.0


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


async def _run_cli():
    """Interactive CLI for testing text agents."""
    _register_defaults()

    agents = list_agents()
    print("\n=== Clairvoyance Text Agent Playground ===\n")
    print("Available agents:")
    for i, agent in enumerate(agents, 1):
        print(f"  {i}. {agent['name']} — {agent['description'][:80]}...")

    if not agents:
        print("  No agents registered.")
        return

    # Select agent
    print()
    if len(agents) == 1:
        selected = agents[0]["name"]
        print(f"Auto-selected: {selected}\n")
    else:
        try:
            choice = input("Select agent number (or name): ").strip()
            if choice.isdigit():
                idx = int(choice) - 1
                selected = agents[idx]["name"]
            else:
                selected = choice
        except (IndexError, ValueError, KeyboardInterrupt):
            print("\nExiting.")
            return

    agent_info = get_agent(selected)
    if not agent_info:
        print(f"Agent '{selected}' not found.")
        return

    print(f"Using agent: {selected}")
    print("Enter your prompt (Ctrl+C to exit):\n")

    try:
        user_input = input("> ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nExiting.")
        return

    if not user_input:
        print("Empty input. Exiting.")
        return

    # Run with streaming if available
    start_time = time.time()

    if agent_info["stream_fn"]:
        print("\n--- Streaming output ---\n")
        async for status in agent_info["stream_fn"](description=user_input):
            stage = status.stage if hasattr(status, "stage") else status.get("stage")
            msg = (
                status.message if hasattr(status, "message") else status.get("message")
            )
            pct = (
                status.progress_pct
                if hasattr(status, "progress_pct")
                else status.get("progress_pct", 0)
            )
            agent_name = (
                status.agent_name
                if hasattr(status, "agent_name")
                else status.get("agent_name")
            )
            elapsed = (
                status.elapsed_secs
                if hasattr(status, "elapsed_secs")
                else status.get("elapsed_secs", 0)
            )

            agent_label = f" [{agent_name}]" if agent_name else ""
            print(f"  [{pct:3d}%] {stage}{agent_label} — {msg} ({elapsed}s)")
    else:
        print("\n--- Running (no streaming) ---\n")
        result = await agent_info["invoke_fn"](description=user_input)
        elapsed = round(time.time() - start_time, 1)
        print(f"\nStatus: {result.get('status')}")
        print(f"Message: {result.get('message')}")
        print(f"Elapsed: {elapsed}s")
        if result.get("result"):
            print(f"\nResult:\n{result['result'][:2000]}")


def main():
    """Playground CLI entrypoint."""
    asyncio.run(_run_cli())


if __name__ == "__main__":
    main()

"""Top-level LangGraph assembly for Blueprint v2.

Two async nodes per turn:

* ``tick`` — :func:`turn.run_turn` does all the work (planning,
  extraction, finalize). Writes state and returns the update dict.
* ``await_approval`` — :func:`turn.await_approval` fires
  :func:`langgraph.types.interrupt` when ``pending_approval_for`` is
  set. The state writes from ``tick`` are already committed by then,
  so chat UIs that poll ``aget_state`` see the flag while programmatic
  SDK consumers can ``Command(resume=...)`` to continue.

A conditional edge from ``tick`` decides whether to enter
``await_approval`` or finish — see :func:`turn.route_after_turn`. The
context schema (``BlueprintContext``) is wired here so node functions
get a typed ``runtime: Runtime[BlueprintContext]`` injected on call.

See ``docs/blueprint/TEMPLATE_CREATION_AGENT.md`` for the architecture overview.
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.ai.text.agents.blueprint.agent.state import BlueprintContext, BlueprintState
from app.ai.text.agents.blueprint.agent.turn import (
    await_approval,
    route_after_turn,
    run_turn,
)


async def _tick(
    state: BlueprintState, runtime: Runtime[BlueprintContext]
) -> dict[str, Any]:
    """Single graph node: route the turn to :func:`run_turn`.

    The runtime carries the session-fixed context (mode, reseller, etc.)
    that used to live on state. See :class:`BlueprintContext`.
    """
    return await run_turn(state, runtime)


def create_blueprint_agent(
    *,
    checkpointer: Optional[BaseCheckpointSaver] = None,
) -> Any:
    """Compile and return the Blueprint LangGraph.

    Args:
        checkpointer: LangGraph checkpointer for session state persistence
            (Postgres in production, MemorySaver in dev/CI).
    """
    graph = StateGraph(BlueprintState, context_schema=BlueprintContext)
    graph.add_node("tick", _tick)
    graph.add_node("await_approval", await_approval)
    graph.add_edge(START, "tick")
    graph.add_conditional_edges(
        "tick",
        route_after_turn,
        {"await_approval": "await_approval", END: END},
    )
    graph.add_edge("await_approval", END)
    return graph.compile(checkpointer=checkpointer)


__all__ = ["create_blueprint_agent"]

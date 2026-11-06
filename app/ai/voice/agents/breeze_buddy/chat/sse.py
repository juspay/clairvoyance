"""SSE wire helpers for the chat router.

Just the event dataclass and the wire formatter — the agent yields
``SSEEvent`` directly, no Frame classifier. Event taxonomy lives in
``docs/CHAT_MODE.md`` §6.4.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SSEEvent:
    event: str
    data: Dict[str, Any] = field(default_factory=dict)
    id: Optional[str] = None


def format_sse(event: SSEEvent) -> str:
    """Render an SSE event in wire format. Caller flushes the stream."""
    lines = [f"event: {event.event}"]
    if event.id is not None:
        lines.append(f"id: {event.id}")
    lines.append("data: " + json.dumps(event.data, ensure_ascii=False))
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Step-progress factories (multi-step UX slice 1).
#
# One step per tool execution today; the shapes are forward-compatible with
# the planner vocabulary we'll extend later (plan_started, parallel steps):
# ``step_started {turn_id?, step_id, label}`` /
# ``step_completed {step_id, status: "ok"|"error", summary?, count?}``.
# ``step_id`` is the tool_call_id, so the widget reconciles in place.
# ---------------------------------------------------------------------------


def step_started_event(
    *, step_id: str, label: str, turn_id: Optional[str] = None
) -> SSEEvent:
    """A step (tool execution) began — the widget shows ``label`` with a
    running affordance."""
    data: Dict[str, Any] = {"step_id": step_id, "label": label}
    if turn_id is not None:
        data["turn_id"] = turn_id
    return SSEEvent(event="step_started", data=data)


def step_completed_event(
    *,
    step_id: str,
    status: str,
    label: Optional[str] = None,
    summary: Optional[str] = None,
    count: Optional[int] = None,
) -> SSEEvent:
    """A step finished — flips the matching line in place. ``label`` carries
    the done-form ("Searched the catalog"); ``summary``/``count`` are the
    optional best-effort result annotation ("6 results")."""
    data: Dict[str, Any] = {"step_id": step_id, "status": status}
    if label is not None:
        data["label"] = label
    if summary is not None:
        data["summary"] = summary
    if count is not None:
        data["count"] = count
    return SSEEvent(event="step_completed", data=data)


def plan_event(
    *, steps: "list[Dict[str, Any]]", turn_id: Optional[str] = None, revised: bool
) -> SSEEvent:
    """The model declared (or revised) its tool plan for this turn.

    ``steps`` = ``[{id, tool, label}]`` in intended execution order —
    ``label`` is the registry running-label so the widget renders pending
    skeleton lines that later step_started events claim (matched by
    label, first-unclaimed-wins). ``plan_updated`` REPLACES the pending
    remainder; started/completed lines are never rewritten.
    """
    data: Dict[str, Any] = {"steps": steps}
    if turn_id is not None:
        data["turn_id"] = turn_id
    return SSEEvent(event="plan_updated" if revised else "plan_started", data=data)


__all__ = [
    "SSEEvent",
    "format_sse",
    "step_started_event",
    "step_completed_event",
    "plan_event",
]

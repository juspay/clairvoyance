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


__all__ = ["SSEEvent", "format_sse"]

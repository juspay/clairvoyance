"""RTVI event emission for global HTTP function SSE streams.

Mirrors each parsed SSE event (and a terminal end marker) to connected Daily
clients as RTVIServerMessages. Emissions are scheduled on the event loop and
never awaited, so RTVI push latency can't block SSE consumption or the
handler's return to the LLM.
"""

import asyncio
from typing import Any, Dict, List

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext

SSE_EVENT_TYPE = "global-function-sse-event"
SSE_END_TYPE = "global-function-sse-end"


def _schedule_emit(
    context: TemplateContext, event_type: str, payload: Dict[str, Any]
) -> None:
    """Fire-and-forget RTVI server message. No-op when off Daily."""
    emit = getattr(context.bot, "_emit_rtvi_event", None)
    if emit is None:
        return
    asyncio.create_task(emit(event_type, payload))


class SseRtviForwarder:
    """Callable that records SSE chunks and streams each to the client."""

    def __init__(self, context: TemplateContext, function_name: str):
        self._context = context
        self._function_name = function_name
        self.chunks: List[Dict[str, Any]] = []

    async def __call__(self, event: Dict[str, Any]) -> None:
        index = len(self.chunks)
        self.chunks.append(event)
        _schedule_emit(
            self._context,
            SSE_EVENT_TYPE,
            {
                "function_name": self._function_name,
                "index": index,
                "event": event,
            },
        )

    def emit_end(self) -> None:
        _schedule_emit(
            self._context,
            SSE_END_TYPE,
            {
                "function_name": self._function_name,
                "total_events": len(self.chunks),
            },
        )

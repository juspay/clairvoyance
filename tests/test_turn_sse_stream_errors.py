"""Regression tests for ``_turn_sse_stream``'s crash path.

The 2026-07-28 live incident: the agent's turn raised a Gemini
``ClientError`` whose text contains a brace dict (``{'message': ...}``).
The except-branch's ``logger.error(..., exc_info=True)`` call — a loguru
misuse: any kwarg makes loguru re-``.format()`` the message — then raised
KeyError from INSIDE the logging call, killing the generator before the
graceful ``error`` + ``turn_end FAILED`` frames went out. The client saw
a raw aborted stream ("network error" / turn_exception) instead of the
designed degradation.
"""

import json
import time
from typing import cast

import pytest

import app.api.routers.breeze_buddy.chat.handlers as ch
from app.ai.voice.agents.breeze_buddy.chat.metrics import TurnMetrics
from app.services.redis.locks import RedisLock


class _StubLock:
    def __init__(self):
        self.released = False

    async def release(self):
        self.released = True


def _parse_frames(chunks):
    """format_sse output → list of (event, data-dict)."""
    frames = []
    for chunk in chunks:
        event = None
        data = None
        for line in chunk.strip().splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
        frames.append((event, data))
    return frames


@pytest.mark.asyncio
async def test_brace_bearing_exception_still_yields_graceful_frames(monkeypatch):
    """An exception whose str contains ``{'message': ...}`` (every Gemini
    API error) must produce the masked ``error`` frame + ``turn_end
    FAILED`` — not crash the generator from inside logging."""

    async def _persist_noop(_metrics):
        return None

    monkeypatch.setattr(ch, "_persist_turn_metrics", _persist_noop)

    async def exploding_events():
        yield ch.SSEEvent(event="assistant_token", data={"text": "hi"})
        raise RuntimeError(
            "400 Bad Request. {'message': '{\"error\": {\"code\": 400}}', "
            "'status': 'Bad Request'}"
        )

    lock = _StubLock()
    metrics = TurnMetrics(session_id="sess-test", template_id=None, t0=time.monotonic())
    chunks = [
        chunk
        async for chunk in ch._turn_sse_stream(
            session_id="sess-test",
            lock=cast(RedisLock, lock),
            turn_metrics=metrics,
            events=exploding_events(),
        )
    ]
    frames = _parse_frames(chunks)
    assert frames[0] == ("assistant_token", {"text": "hi"})
    assert frames[1][0] == "error"
    # Provider internals must be masked, not leaked.
    assert frames[1][1] == {"code": "internal", "message": "Internal server error"}
    assert frames[2] == ("turn_end", {"session_status": "FAILED"})
    assert metrics.status == "FAILED"
    assert lock.released

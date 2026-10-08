"""An edge function that ends the call leaves pipecat-flows' node-switch
follow-up pending on the assistant aggregator; teardown must cancel it so the
GC never reports a destroyed pending task."""

import asyncio
from types import SimpleNamespace

import pytest

from app.ai.voice.agents.breeze_buddy.agent.pipeline import (
    cancel_orphaned_transition_tasks,
)


class _Assistant:
    def __init__(self, tasks):
        self._context_updated_tasks = set(tasks)
        self.cancelled = []

    async def cancel_task(self, task, timeout=1.0):
        self.cancelled.append(task)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _never():
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_pending_follow_up_is_cancelled():
    pending = asyncio.create_task(
        _never(), name="not_interested:call_1:on_context_updated"
    )
    done = asyncio.create_task(asyncio.sleep(0))
    await done  # finished before teardown looks at the set
    assistant = _Assistant([pending, done])

    await cancel_orphaned_transition_tasks(SimpleNamespace(assistant=lambda: assistant))

    assert assistant.cancelled == [pending]  # the finished one is left alone
    assert pending.cancelled()


@pytest.mark.asyncio
async def test_no_aggregator_or_no_set_is_a_noop():
    await cancel_orphaned_transition_tasks(None)
    bare = SimpleNamespace(assistant=lambda: SimpleNamespace())
    await cancel_orphaned_transition_tasks(
        bare
    )  # renamed upstream attr: degrade, don't crash


@pytest.mark.asyncio
async def test_cleanup_is_best_effort():
    pending = asyncio.create_task(_never())

    class _Broken:
        _context_updated_tasks = {pending}

        async def cancel_task(self, task, timeout=1.0):
            raise RuntimeError("TaskManager is still not initialized")

    # must not raise: both nets run inside teardown / an event handler
    await cancel_orphaned_transition_tasks(SimpleNamespace(assistant=lambda: _Broken()))
    pending.cancel()

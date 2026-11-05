"""An edge function that ends the call leaves pipecat-flows' node-switch
follow-up pending on the assistant aggregator; the aggregator's cleanup()
(run by the pipeline before PipelineTask checks for dangling tasks) must
cancel it so the GC never reports a destroyed pending task."""

import asyncio

import pytest
from pipecat.frames.frames import (
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    FunctionCallResultProperties,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams

from app.ai.voice.agents.breeze_buddy.agent.pipeline import (
    BreezeAssistantAggregator,
    ContextAggregatorPair,
)


async def _never():
    await asyncio.Event().wait()


def _assistant() -> BreezeAssistantAggregator:
    assistant = ContextAggregatorPair(LLMContext()).assistant()
    assert isinstance(assistant, BreezeAssistantAggregator)  # the pair wires ours in
    assert str(assistant).startswith("LLMAssistantAggregator#")  # stock log name
    manager = TaskManager()
    manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    assistant._task_manager = manager  # what PipelineTask.setup() would do
    return assistant


async def _edge_function_result(assistant: BreezeAssistantAggregator) -> None:
    """Drive the real pipecat path: an edge function's result carries
    on_context_updated (pipecat-flows' node switch), which the aggregator
    schedules as the follow-up task this fix reaps."""
    await assistant.process_frame(
        FunctionCallInProgressFrame(
            function_name="not_interested", tool_call_id="call_1", arguments={}
        ),
        FrameDirection.DOWNSTREAM,
    )
    await assistant.process_frame(
        FunctionCallResultFrame(
            function_name="not_interested",
            tool_call_id="call_1",
            arguments={},
            result={"status": "acknowledged"},
            properties=FunctionCallResultProperties(
                run_llm=False, on_context_updated=_never
            ),
        ),
        FrameDirection.DOWNSTREAM,
    )
    await asyncio.sleep(0)  # let the follow-up start


@pytest.mark.asyncio
async def test_cleanup_cancels_the_follow_up_pipecat_scheduled():
    assistant = _assistant()
    await _edge_function_result(assistant)
    (follow_up,) = assistant._context_updated_tasks  # the real pipecat attribute
    assert "not_interested:call_1:on_context_updated" in follow_up.get_name()
    done = assistant.create_task(asyncio.sleep(0), "finished")
    await done
    assistant._context_updated_tasks.add(done)

    await assistant.cleanup()

    assert follow_up.cancelled()
    assert done.done() and not done.cancelled()


@pytest.mark.asyncio
async def test_cleanup_is_best_effort(monkeypatch: pytest.MonkeyPatch):
    assistant = _assistant()
    first = assistant.create_task(_never(), "first")
    second = assistant.create_task(_never(), "second")
    assistant._context_updated_tasks.update({first, second})
    await asyncio.sleep(0)  # let both start: cancelling an unstarted task warns

    real_cancel = assistant.cancel_task

    async def cancel_or_blow_up(task, timeout=1.0):
        if task is first:
            raise RuntimeError("TaskManager is still not initialized")
        await real_cancel(task, timeout)

    monkeypatch.setattr(assistant, "cancel_task", cancel_or_blow_up)

    await assistant.cleanup()  # must not raise: one failure doesn't skip the rest

    assert second.cancelled()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

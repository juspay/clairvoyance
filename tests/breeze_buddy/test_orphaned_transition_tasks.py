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
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregator,
    LLMContextAggregatorPair,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams


async def _never():
    await asyncio.Event().wait()


def _assistant() -> LLMAssistantAggregator:
    assistant = LLMContextAggregatorPair(LLMContext()).assistant()
    assert str(assistant).startswith("LLMAssistantAggregator#")  # stock log name
    manager = TaskManager()
    manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    assistant._task_manager = manager  # what PipelineTask.setup() would do
    return assistant


async def _edge_function_result(assistant: LLMAssistantAggregator) -> None:
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

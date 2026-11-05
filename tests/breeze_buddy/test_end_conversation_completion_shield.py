"""end_conversation's DB completion must survive the handler being cancelled.

Production (2026-10-14, call e3a0c4ae…): after a legacy Plivo transfer the
pipeline tore down while ``end_conversation`` was still awaiting
``handle_call_completion`` (channel decrement in flight). The function-call
task was cancelled, ``CancelledError`` skipped ``except Exception`` and ran
``finally`` (EndFrame), so the lead stayed PROCESSING until the stuck
sweeper closed it as UNKNOWN 30 min later and the number's channel stayed
counted the whole time.
"""

import asyncio
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from pipecat.frames.frames import EndFrame

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext

end_conversation_module = import_module(
    "app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation"
)


def _bot(completion, *, transport_type="plivo", call_sid="call-sid-1", task=None):
    return SimpleNamespace(
        approval_manager=None,
        call_sid=call_sid,
        completion_function=completion,
        configurations=SimpleNamespace(knowledge_base=None),
        context=SimpleNamespace(messages=[{"role": "user", "content": "hi"}]),
        conversation_ended=False,
        end_conversation_callbacks=[],
        errors=[],
        lead=SimpleNamespace(
            id="lead-id", metaData={}, outcome="TRANSFER_REQUESTED", payload={}
        ),
        metrics_collector=None,
        pending_transfer=None,
        prior_generation_messages=[],
        task=task,
        transport_type=transport_type,
    )


@pytest.fixture(autouse=True)
def _quiet_side_effects(monkeypatch):
    monkeypatch.setattr(
        end_conversation_module, "enqueue_conversation_evaluation", AsyncMock()
    )
    monkeypatch.setattr(
        end_conversation_module, "update_span_with_evaluation_data", lambda _c: None
    )


class _BlockingCompletion:
    """Stands in for handle_call_completion: blocks mid-way (the channel
    decrement DB call), then records the final DB update."""

    def __init__(self):
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.done = asyncio.Event()
        self.db_update = AsyncMock(return_value=None)
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        await self.gate.wait()
        await self.db_update(kwargs["call_id"])
        self.done.set()
        return None


@pytest.mark.parametrize(
    "transport_type, call_sid, expected_call_id",
    [("plivo", "call-sid-1", "call-sid-1"), ("daily", None, "lead-id")],
)
async def test_cancel_during_completion_still_finishes_the_db_write(
    transport_type, call_sid, expected_call_id
):
    completion = _BlockingCompletion()
    pipeline_task = SimpleNamespace(queue_frame=AsyncMock())
    context = TemplateContext(
        _bot(
            completion,
            transport_type=transport_type,
            call_sid=call_sid,
            task=pipeline_task,
        )
    )

    handler = asyncio.create_task(end_conversation_module.end_conversation(context, {}))
    await asyncio.wait_for(completion.started.wait(), 1)

    # The transfer moved the leg off our websocket: pipeline teardown cancels
    # the in-flight function-call task.
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler

    # The handler is gone, but the completion must still be alive…
    assert completion.db_update.await_count == 0
    completion.gate.set()
    await asyncio.wait_for(completion.done.wait(), 1)

    # …and run to the end: the lead row gets FINISHED + channel decremented.
    completion.db_update.assert_awaited_once_with(expected_call_id)
    assert completion.calls[0]["call_id"] == expected_call_id
    # finally-block behaviour is unchanged: EndFrame still goes out.
    pipeline_task.queue_frame.assert_awaited_once()
    assert isinstance(pipeline_task.queue_frame.await_args.args[0], EndFrame)


async def test_normal_completion_path_unchanged():
    updated = SimpleNamespace(id="lead-id", template_id=None, outcome="FINISHED")
    completion = AsyncMock(return_value=updated)
    pipeline_task = SimpleNamespace(queue_frame=AsyncMock())
    context = TemplateContext(_bot(completion, task=pipeline_task))

    result = await end_conversation_module.end_conversation(context, {})

    assert result == {}
    completion.assert_awaited_once_with(
        call_id="call-sid-1",
        outcome="TRANSFER_REQUESTED",
        call_end_time=ANY,
        meta_data=ANY,
    )
    assert context.lead is updated  # the returned lead replaces the local one
    pipeline_task.queue_frame.assert_awaited_once()


async def test_completion_failure_is_still_logged_not_raised():
    completion = AsyncMock(side_effect=RuntimeError("db down"))
    pipeline_task = SimpleNamespace(queue_frame=AsyncMock())
    context = TemplateContext(_bot(completion, task=pipeline_task))

    result = await end_conversation_module.end_conversation(context, {})

    assert result == {}  # swallowed by `except Exception`, as before
    pipeline_task.queue_frame.assert_awaited_once()

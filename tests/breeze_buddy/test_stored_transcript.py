import asyncio
import importlib
import json
from types import SimpleNamespace
from typing import cast

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.utils.stored_transcript import generation_rows

end_conversation_module = importlib.import_module(
    "app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation"
)

PERSONA = "You are Ravi, a support agent from Test Store."


async def test_the_stored_transcript_keeps_todays_shape(monkeypatch):
    async def completion_function(**kwargs):
        return None

    monkeypatch.setattr(
        end_conversation_module, "update_span_with_evaluation_data", lambda c: None
    )
    started = {"type": "async_tool", "status": "running", "tool_call_id": "c2"}
    finished = {"type": "async_tool", "status": "finished", "tool_call_id": "c2"}
    lead = SimpleNamespace(
        id="lead-1",
        outcome="CONFIRMED",
        payload={},
        metaData={"node_traversal": [{"node_name": "greet", "template_id": "t-1"}]},
    )
    context = SimpleNamespace(
        conversation_ended=False,
        call_sid="call-1",
        lead=lead,
        bot=SimpleNamespace(
            pending_transfer=None,
            prior_generation_messages=[],
            flow_config={"nodes": {"greet": {"role_message": PERSONA}}},
            template=SimpleNamespace(id="t-1"),
            errors=[],
        ),
        configurations=None,
        context=SimpleNamespace(
            messages=[
                {"role": "assistant", "content": "Hello Rohit"},
                {"role": "developer", "content": "Greet Rohit."},
                {"role": "user", "content": "Haan"},
                {"role": "tool", "content": "COMPLETED", "tool_call_id": "c1"},
                {"role": "tool", "content": json.dumps(started), "tool_call_id": "c2"},
                {"role": "developer", "content": json.dumps(finished)},
                {
                    "role": "tool",
                    "content": '{"status": "success"}',
                    "tool_call_id": "c3",
                },
            ]
        ),
        record_node_exit=lambda: None,
        completion_function=completion_function,
        end_conversation_callbacks=[],
        task=None,
    )

    await end_conversation_module.end_conversation(cast(TemplateContext, context), {})

    assert lead.metaData["transcription"] == [
        {"role": "system", "content": PERSONA},
        {"role": "assistant", "content": "Hello Rohit"},
        {"role": "system", "content": "Greet Rohit."},
        {"role": "user", "content": "Haan"},
        {"role": "tool", "content": '{"status": "success"}'},
    ]


def test_an_outgoing_agents_rows_keep_the_final_generations_shape():
    bot = SimpleNamespace(
        flow_config={"nodes": {"greet": {"role_message": PERSONA}}},
        template=SimpleNamespace(id="t-1"),
    )
    lead = SimpleNamespace(
        metaData={"node_traversal": [{"node_name": "greet", "template_id": "t-1"}]}
    )
    messages = [
        {"role": "developer", "content": "KB: returns within 7 days"},
        {"role": "developer", "content": "Greet Rohit."},
        {"role": "assistant", "content": "Namaste"},
        {"role": "user", "content": "Haan"},
        {"role": "tool", "tool_call_id": "c1", "content": "COMPLETED"},
    ]

    rows = generation_rows(bot, lead, messages, "KB:")

    assert rows == [
        {"role": "system", "content": PERSONA},
        {"role": "system", "content": "Greet Rohit."},
        {"role": "assistant", "content": "Namaste"},
        {"role": "user", "content": "Haan"},
    ]
    assert all(row["role"] != "developer" for row in rows)


async def test_a_cancelled_end_conversation_call_still_finishes_finalizing(monkeypatch):
    finished = []

    async def slow_finalize(context, args):
        await asyncio.sleep(0.05)
        finished.append(context)
        return {}

    monkeypatch.setattr(end_conversation_module, "_finalize_call", slow_finalize)
    call = asyncio.create_task(
        end_conversation_module.end_conversation(cast(TemplateContext, "ctx"), {})
    )
    await asyncio.sleep(0)
    call.cancel()
    await asyncio.gather(call, return_exceptions=True)
    assert finished == []

    await asyncio.sleep(0.1)
    assert finished == ["ctx"]

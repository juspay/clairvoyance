import asyncio
from types import SimpleNamespace

import pytest
from pipecat.processors.aggregators.llm_context import LLMContext

from app.ai.voice.agents.breeze_buddy.observers import (
    factory as observer_factory,
    manager as observer_manager,
)
from app.ai.voice.agents.breeze_buddy.observers.observer import (
    RealtimeObserver,
    _build_tool_from_action,
)
from app.ai.voice.agents.breeze_buddy.template.types import ObserverConfig


def _observer_config(**overrides):
    data = {
        "name": "voicemail_detector",
        "system_prompt": "Detect voicemail.",
        "action": {"type": "function", "handler": "end_conversation"},
    }
    data.update(overrides)
    return ObserverConfig.model_validate(data)


async def test_build_observers_skips_disabled_config(monkeypatch):
    async def fail_get_llm_service(*args, **kwargs):
        raise AssertionError("disabled observers should not create LLM services")

    monkeypatch.setattr(observer_factory, "get_llm_service", fail_get_llm_service)

    observers = await observer_factory.build_observers(
        configs=[_observer_config(enabled=False)],
        template=None,
        agent_context=object(),
        handler_map={},
    )

    assert observers == []


def test_action_derived_tool_uses_function_handler_with_empty_schema():
    tool = _build_tool_from_action(
        _observer_config(action={"type": "function", "handler": "end_conversation"})
    )[0]

    assert tool.name == "end_conversation"
    assert tool.properties == {}
    assert tool.required == []


def test_action_derived_tool_uses_action_type_for_alert():
    tool = _build_tool_from_action(_observer_config(action={"type": "alert"}))[0]

    assert tool.name == "alert"
    assert tool.properties == {}
    assert tool.required == []


def test_action_derived_tool_uses_handler_for_alert_when_configured():
    tool = _build_tool_from_action(
        _observer_config(action={"type": "alert", "handler": "send_alert"})
    )[0]

    assert tool.name == "send_alert"
    assert tool.properties == {}
    assert tool.required == []


def test_action_derived_tool_uses_configured_description():
    tool = _build_tool_from_action(
        _observer_config(
            action={
                "type": "function",
                "handler": "end_conversation",
                "description": "End the call when voicemail is detected.",
            }
        )
    )[0]

    assert tool.description == "End the call when voicemail is detected."


def test_action_derived_tool_has_description_fallback():
    tool = _build_tool_from_action(_observer_config(action={"type": "alert"}))[0]

    assert tool.description == "Call this when the observer condition is detected."


async def test_alert_observer_dispatches_through_send_alert_handler():
    captured = {}

    async def send_alert(args):
        captured.update(args)

    lead = SimpleNamespace(metaData=None, outcome=None)
    agent_context = SimpleNamespace(
        lead=lead,
        call_sid="CA123",
        flow_manager=SimpleNamespace(current_node="greeting"),
    )
    observer = RealtimeObserver(
        _observer_config(action={"type": "alert", "handler": "send_alert"}),
        llm_service=object(),
        agent_context=agent_context,
        handler_map={"send_alert": send_alert},
    )
    observer._last_detection = {"reason": "voicemail"}

    await observer.execute_action()

    assert captured["source"] == "observer"
    assert captured["observer_name"] == "voicemail_detector"
    assert captured["call_sid"] == "CA123"
    assert captured["detection"] == {"reason": "voicemail"}
    assert captured["title"] == "Breeze Buddy - Observer: voicemail_detector"
    assert lead.metaData["observer_triggered"] == "voicemail_detector"


async def test_observer_manager_stop_waits_before_cancel(monkeypatch):
    manager = observer_manager.ObserverManager(observers=[], llm_context=LLMContext())

    async def never_finishes():
        await asyncio.Event().wait()

    task = asyncio.create_task(never_finishes())
    manager._track_task(task)
    wait_call = {}

    async def fake_wait(tasks, timeout):
        wait_call["tasks"] = tasks
        wait_call["timeout"] = timeout
        return set(), set(tasks)

    monkeypatch.setattr(observer_manager.asyncio, "wait", fake_wait)

    await manager.stop()
    await asyncio.sleep(0)

    assert wait_call["tasks"] == {task}
    assert wait_call["timeout"] == 15
    assert task.cancelled()
    assert manager._pending == set()

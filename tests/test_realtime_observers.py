import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List, cast

import pytest
from fastapi import HTTPException
from pipecat.processors.aggregators.llm_context import LLMContext

from app.ai.voice.agents.breeze_buddy.observers import (
    factory as observer_factory,
    manager as observer_manager,
)
from app.ai.voice.agents.breeze_buddy.observers.observer import (
    RealtimeObserver,
    _bounded_detection,
    _build_tool_from_action,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ObserverConfig,
    TemplateModel,
)
from app.api.routers.breeze_buddy.templates import handlers as template_handlers


def _legacy_template():
    """Template stub carrying one legacy observer in ``configurations.observers``.

    ``resolve_observer_configs`` only reads ``id`` and ``configurations.observers``,
    so a namespace stands in for the full TemplateModel — cast so the checker
    accepts it at the call site.
    """
    return cast(
        TemplateModel,
        SimpleNamespace(
            id="template-1",
            configurations=SimpleNamespace(observers=[_observer_config(name="legacy")]),
        ),
    )


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


async def test_resolve_observer_configs_prefers_evaluation_config(monkeypatch):
    async def fake_get_observer_evaluation_config(template_id):
        assert template_id == "template-1"
        return {
            "id": "config-1",
            "enabled": True,
            "configuration": {
                "observers": [
                    {
                        "name": "table_observer",
                        "system_prompt": "Detect from table.",
                        "action": {"type": "alert", "handler": "send_alert"},
                    }
                ]
            },
        }

    monkeypatch.setattr(
        observer_factory,
        "get_observer_evaluation_config",
        fake_get_observer_evaluation_config,
    )
    template = _legacy_template()

    configs, config_id = await observer_factory.resolve_observer_configs(template)

    assert [config.name for config in configs] == ["table_observer"]
    assert config_id == "config-1"


async def test_resolve_observer_configs_disabled_table_config_blocks_legacy(
    monkeypatch,
):
    async def fake_get_observer_evaluation_config(template_id):
        return {"id": "config-1", "enabled": False, "configuration": {"observers": []}}

    monkeypatch.setattr(
        observer_factory,
        "get_observer_evaluation_config",
        fake_get_observer_evaluation_config,
    )
    template = _legacy_template()

    assert await observer_factory.resolve_observer_configs(template) == ([], "config-1")


async def test_resolve_observer_configs_falls_back_to_legacy_when_row_missing(
    monkeypatch,
):
    async def fake_get_observer_evaluation_config(template_id):
        return None

    monkeypatch.setattr(
        observer_factory,
        "get_observer_evaluation_config",
        fake_get_observer_evaluation_config,
    )
    template = _legacy_template()

    configs, config_id = await observer_factory.resolve_observer_configs(template)

    assert [config.name for config in configs] == ["legacy"]
    assert config_id is None


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


async def test_observer_action_records_detection_result(monkeypatch):
    saved = {}
    called = {}

    async def fake_save_evaluation_results(**kwargs):
        saved.update(kwargs)

    async def noop(args):
        called.update(args)

    monkeypatch.setattr(
        "app.ai.voice.agents.breeze_buddy.observers.utils.save_evaluation_results",
        fake_save_evaluation_results,
    )
    started_at = datetime(2026, 8, 9, tzinfo=timezone.utc)
    lead = SimpleNamespace(
        id="lead-1",
        call_id="CA123",
        reseller_id="reseller-1",
        merchant_id="merchant-1",
        template_id="template-1",
        call_initiated_time=started_at,
        metaData={},
        outcome=None,
    )
    agent_context = SimpleNamespace(
        lead=lead,
        template=SimpleNamespace(id="template-1"),
        call_sid="CA123",
        flow_manager=SimpleNamespace(current_node="greeting"),
    )
    observer = RealtimeObserver(
        _observer_config(action={"type": "function", "handler": "noop"}),
        llm_service=object(),
        agent_context=agent_context,
        handler_map={"noop": noop},
        evaluation_config_id="config-1",
    )
    observer._last_detection = {"reason": "voicemail"}

    await observer.execute_action()

    assert called["detection"] == {"reason": "voicemail"}
    assert saved["source_id"] == "lead-1"
    assert saved["reseller_id"] == "reseller-1"
    assert saved["merchant_id"] == "merchant-1"
    assert saved["template_id"] == "template-1"
    assert saved["started_at"] == started_at
    assert saved["evaluation_type"] == "OBSERVER"
    assert saved["evaluation_config_id"] == "config-1"
    assert saved["results"][0]["type"] == "voicemail_detector"
    assert saved["results"][0]["action_type"] == "function"
    assert saved["results"][0]["handler"] == "noop"
    assert saved["results"][0]["detection"] == {"reason": "voicemail"}


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


async def test_sync_observer_evaluation_config_writes_observers(monkeypatch):
    calls = []

    async def fake_upsert(template_id, configuration, enabled):
        calls.append((template_id, configuration, enabled))

    monkeypatch.setattr(
        template_handlers, "upsert_observer_evaluation_config", fake_upsert
    )

    await template_handlers.sync_observer_evaluation_config(
        "template-1", {"observers": [{"name": "voicemail_detector"}]}
    )

    assert calls == [
        ("template-1", {"observers": [{"name": "voicemail_detector"}]}, True)
    ]


async def test_sync_observer_evaluation_config_writes_empty_list(monkeypatch):
    calls = []

    async def fake_upsert(template_id, configuration, enabled):
        calls.append(configuration)

    monkeypatch.setattr(
        template_handlers, "upsert_observer_evaluation_config", fake_upsert
    )

    await template_handlers.sync_observer_evaluation_config(
        "template-1", {"observers": []}
    )

    assert calls == [{"observers": []}]


async def test_sync_observer_evaluation_config_skips_when_field_absent(monkeypatch):
    async def fail_upsert(*args, **kwargs):
        raise AssertionError("a PUT without observers must not touch the row")

    monkeypatch.setattr(
        template_handlers, "upsert_observer_evaluation_config", fail_upsert
    )

    await template_handlers.sync_observer_evaluation_config("template-1", {"llm": {}})
    await template_handlers.sync_observer_evaluation_config("template-1", None)


async def test_sync_observer_evaluation_config_raises_on_upsert_failure(monkeypatch):
    """Silence here would report a config as live while calls use the old one."""

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(template_handlers, "upsert_observer_evaluation_config", boom)

    with pytest.raises(HTTPException) as excinfo:
        await template_handlers.sync_observer_evaluation_config(
            "template-1", {"observers": []}
        )

    assert excinfo.value.status_code == 500
    assert "observer configuration" in excinfo.value.detail


async def test_run_checks_fires_alert_before_terminal_observer():
    """A terminal action must not swallow an alert detected in the same batch.

    Config order puts the end_conversation observer first, which is exactly the
    case that used to return out of the loop before the alert ran.
    """
    executed = []

    def _fake_observer(name, action):
        observer = SimpleNamespace(
            name=name,
            config=ObserverConfig.model_validate(
                {
                    "name": name,
                    "system_prompt": "detect",
                    "action": action,
                    "trigger_on": ["on_user_turn_stopped"],
                }
            ),
        )

        async def check(_transcript):
            return True

        async def execute_action():
            executed.append(name)

        observer.check = check
        observer.execute_action = execute_action
        return observer

    manager = observer_manager.ObserverManager(
        observers=cast(
            List[RealtimeObserver],
            [
                _fake_observer(
                    "terminal", {"type": "function", "handler": "end_conversation"}
                ),
                _fake_observer("alerting", {"type": "alert", "handler": "send_alert"}),
            ],
        ),
        llm_context=LLMContext(),
    )

    await manager._run_checks("on_user_turn_stopped")

    assert executed == ["alerting", "terminal"]


def test_bounded_detection_keeps_only_known_scalar_fields():
    out = _bounded_detection(
        {
            "reason": "greeting mentions leaving a message",
            "confidence": 0.91,
            "transcript": "customer said their card number is 4111...",
            "customer_name": "Gagan",
        }
    )

    assert out["reason"] == "greeting mentions leaving a message"
    assert out["confidence"] == 0.91
    assert "transcript" not in out
    assert "customer_name" not in out
    assert out["dropped_keys"] == ["customer_name", "transcript"]


def test_bounded_detection_truncates_long_values():
    out = _bounded_detection({"reason": "x" * 5000})

    assert len(out["reason"]) == 300


def test_bounded_detection_handles_non_dict_and_empty():
    assert _bounded_detection(None) == {}
    assert _bounded_detection("voicemail") == {}
    assert _bounded_detection({}) == {}
    assert _bounded_detection({"reason": "   "}) == {}


async def test_sync_observer_evaluation_config_can_swallow_on_create(monkeypatch):
    """Create already committed the template; a 500 would make the caller retry
    into the 409 duplicate guard, and the missing row only costs recording."""

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(template_handlers, "upsert_observer_evaluation_config", boom)

    await template_handlers.sync_observer_evaluation_config(
        "template-1", {"observers": []}, raise_on_failure=False
    )

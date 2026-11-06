"""HITL approval gate — gate_global_function, _make_global_wrapper,
and _flows_async_kwargs.

Companion: [[template/approval.py]], [[template/global_function.py]] and
the plan's review findings (additive watchdog budget; daily-without-channel
must DENY; deny path must skip filler and post-actions). The voice
ApprovalManager lifecycle is covered in tests/test_approval_manager.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.template.approval import (
    ApprovalOutcome,
    gate_global_function,
)
from app.ai.voice.agents.breeze_buddy.template.global_function import (
    _flows_async_kwargs,
    _make_global_wrapper,
)
from app.ai.voice.agents.breeze_buddy.template.types import GlobalBuiltinFunction


def _builtin(approval: Optional[Dict[str, Any]] = None, **overrides):
    cfg: Dict[str, Any] = {
        "type": "builtin",
        "name": "issue_refund",
        "description": "Issue a refund",
        "handler": "issue_refund_handler",
    }
    if approval is not None:
        cfg["approval"] = approval
    cfg.update(overrides)
    return GlobalBuiltinFunction.model_validate(cfg)


class _FakeManager:
    """Scripted ApprovalManager stand-in for gate tests."""

    def __init__(self, outcome: ApprovalOutcome):
        self.outcome = outcome
        self.requests: List[Dict[str, Any]] = []

    async def request(self, function_name, arguments, config):
        self.requests.append(
            {"function_name": function_name, "arguments": arguments, "config": config}
        )
        return self.outcome


class _FakeTask:
    def __init__(self):
        self.frames: List[Any] = []

    async def queue_frame(self, frame):
        self.frames.append(frame)


async def _execute_recorder(record: List[str], result: Any = {"ok": True}):
    async def execute():
        record.append("executed")
        return result

    return execute


# ---------------------------------------------------------------------------
# gate_global_function
# ---------------------------------------------------------------------------


async def test_ungated_function_executes_directly():
    record: List[str] = []
    execute = await _execute_recorder(record)
    result = await gate_global_function(SimpleNamespace(), _builtin(), {}, execute)
    assert record == ["executed"]
    assert result == {"ok": True}


async def test_chat_agent_bypasses_in_handler_gate():
    """ChatAgent gates pre-dispatch — the wrapper gate must pass through."""
    record: List[str] = []
    execute = await _execute_recorder(record)
    bot = SimpleNamespace(handles_approval_externally=True, approval_manager=None)
    func = _builtin(approval={"prompt": "ok?"})
    result = await gate_global_function(bot, func, {}, execute)
    assert record == ["executed"]
    assert result == {"ok": True}


async def test_daily_without_channel_denies():
    """A daily bot with no approval manager (RTVI off) must DENY — the
    surface that's supposed to show an approval UI never auto-executes."""
    record: List[str] = []
    execute = await _execute_recorder(record)
    bot = SimpleNamespace(is_daily_mode=True)
    func = _builtin(approval={"prompt": "ok?", "on_no_channel": "execute"})
    result = await gate_global_function(bot, func, {}, execute)
    assert record == []
    assert result == {"status": "denied", "reason": "approval_channel_unavailable"}


async def test_telephony_on_no_channel_execute():
    record: List[str] = []
    execute = await _execute_recorder(record)
    bot = SimpleNamespace(is_daily_mode=False)
    func = _builtin(approval={"prompt": "ok?"})  # default on_no_channel=execute
    result = await gate_global_function(bot, func, {"a": 1}, execute)
    assert record == ["executed"]
    assert result == {"ok": True}


async def test_telephony_on_no_channel_deny():
    record: List[str] = []
    execute = await _execute_recorder(record)
    bot = SimpleNamespace(is_daily_mode=False)
    func = _builtin(approval={"on_no_channel": "deny"})
    result = await gate_global_function(bot, func, {}, execute)
    assert record == []
    assert result == {"status": "denied", "reason": "no_approval_channel"}


async def test_voice_approve_executes():
    record: List[str] = []
    execute = await _execute_recorder(record)
    manager = _FakeManager(ApprovalOutcome(approved=True, status="approved"))
    bot = SimpleNamespace(is_daily_mode=True, approval_manager=manager, task=None)
    func = _builtin(approval={"prompt": "refund ok?"})
    result = await gate_global_function(bot, func, {"x": 1}, execute)
    assert record == ["executed"]
    assert result == {"ok": True}
    assert manager.requests[0]["function_name"] == "issue_refund"
    assert manager.requests[0]["arguments"] == {"x": 1}


async def test_voice_deny_returns_llm_visible_denial():
    record: List[str] = []
    execute = await _execute_recorder(record)
    manager = _FakeManager(
        ApprovalOutcome(approved=False, status="denied", reason="user said no")
    )
    bot = SimpleNamespace(is_daily_mode=True, approval_manager=manager, task=None)
    func = _builtin(approval={"prompt": "ok?"})
    result = await gate_global_function(bot, func, {}, execute)
    assert record == []
    assert result == {"status": "denied", "reason": "user said no"}


async def test_voice_timeout_maps_to_timeout_status():
    execute = await _execute_recorder([])
    manager = _FakeManager(
        ApprovalOutcome(
            approved=False, status="timeout", reason="approval request timed out"
        )
    )
    bot = SimpleNamespace(is_daily_mode=True, approval_manager=manager, task=None)
    func = _builtin(approval={})
    result = await gate_global_function(bot, func, {}, execute)
    assert result["status"] == "timeout"


async def test_voice_announce_queued_before_wait():
    manager = _FakeManager(ApprovalOutcome(approved=False, status="denied"))
    task = _FakeTask()
    bot = SimpleNamespace(is_daily_mode=True, approval_manager=manager, task=task)
    func = _builtin(approval={"voice_announce": "I need your approval."})
    await gate_global_function(bot, func, {}, await _execute_recorder([]))
    assert len(task.frames) == 1
    assert task.frames[0].text == "I need your approval."


# ---------------------------------------------------------------------------
# _make_global_wrapper — deny must skip filler + post-actions entirely
# ---------------------------------------------------------------------------


async def test_wrapper_denied_call_never_invokes_handler():
    calls: List[Any] = []

    async def wrapped_handler(llm_args, function_config=None):
        calls.append(llm_args)
        return {"ok": True}

    manager = _FakeManager(ApprovalOutcome(approved=False, status="denied"))
    bot = SimpleNamespace(is_daily_mode=True, approval_manager=manager, task=None)
    func = _builtin(approval={"prompt": "ok?"})
    wrapper = _make_global_wrapper(func, wrapped_handler, bot)
    result = await wrapper({"q": 1}, None)
    assert calls == []
    assert result["status"] == "denied"


async def test_wrapper_approved_call_invokes_handler_with_config():
    seen: List[Any] = []

    async def wrapped_handler(llm_args, function_config=None):
        seen.append((llm_args, function_config))
        return {"done": 1}

    manager = _FakeManager(ApprovalOutcome(approved=True, status="approved"))
    bot = SimpleNamespace(is_daily_mode=True, approval_manager=manager, task=None)
    func = _builtin(approval={})
    wrapper = _make_global_wrapper(func, wrapped_handler, bot)
    result = await wrapper({"q": 1}, None)
    assert result == {"done": 1}
    assert seen[0][0] == {"q": 1}
    assert seen[0][1] is func


# ---------------------------------------------------------------------------
# _flows_async_kwargs — ADDITIVE watchdog budget (never max())
# ---------------------------------------------------------------------------


def test_async_kwargs_passthrough_without_approval():
    func = _builtin(timeout_secs=42.0)
    assert _flows_async_kwargs(func)["timeout_secs"] == 42.0


def test_async_kwargs_additive_budget_default_exec():
    func = _builtin(approval={"timeout_secs": 120})
    # 120 (wait) + 60 (exec fallback) + 15 (margin)
    assert _flows_async_kwargs(func)["timeout_secs"] == 195.0


def test_async_kwargs_additive_budget_with_func_timeout():
    # A 119s approval wait must still leave the author's full 90s exec
    # budget — max() would not (the review's watchdog finding).
    func = _builtin(timeout_secs=90.0, approval={"timeout_secs": 120})
    assert _flows_async_kwargs(func)["timeout_secs"] == 120 + 90 + 15

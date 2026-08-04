# pyrefly: ignore-errors
# Duck-typed monkeypatching over ChatAgent internals — same limitation as
# tests/test_agent_step_events.py (whose harness this mirrors).
"""Phase-2 loop mechanics: tool annotations, read-only parallel fan-out,
and deterministic verification hooks.

- Annotation resolution: template override > flavor registry > the
  'destructive' safe default.
- Fan-out: a cycle whose ungated calls are ALL read_only dispatches them
  CONCURRENTLY; mixed batches and the template kill-switch stay strictly
  sequential; tool results enter the LLM context in ORIGINAL call order
  regardless of completion order.
- Verification: a failing post-condition converts the result into the
  structured error envelope before the context/reducers/binding store see
  it, and flips the step line to error; a RAISING verifier fails open.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List

from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import LLMContext

import app.ai.voice.agents.breeze_buddy.chat.agent as agent_module
from app.ai.voice.agents.breeze_buddy.chat.agent import (
    approval as agent_approval,
    context as agent_context,
    core as agent_core,
    cycle as agent_cycle,
    render_ui as agent_render_ui,
    tooling as agent_tooling,
)
from app.ai.voice.agents.breeze_buddy.chat.llm import driver as llm_driver

# agent.py is a package of subsystem modules now — a patched seam must
# land on every submodule that calls it (autoflake prunes unused
# imports per module, hence the hasattr guard).
_AGENT_MODULES = (
    agent_core,
    agent_cycle,
    agent_approval,
    agent_render_ui,
    agent_context,
    agent_tooling,
)


def _patch_agent_attr(monkeypatch, name, value):
    for _mod in _AGENT_MODULES:
        if hasattr(_mod, name):
            monkeypatch.setattr(_mod, name, value)


from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent, _PreparedTools
from app.ai.voice.agents.breeze_buddy.chat.steps import (
    verification as verification_module,
)
from app.ai.voice.agents.breeze_buddy.chat.steps.verification import (
    register_tool_verifier,
    run_tool_verifiers,
)
from app.ai.voice.agents.breeze_buddy.chat.tools.annotations import (
    register_tool_annotations,
    resolve_tool_annotation,
)
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel

# Importing the commerce schemas registers the flavor's annotations +
# verifiers (search_catalog read_only, cart mutation post-conditions).
import app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas  # noqa: F401  isort: skip

_NODE: Dict[str, Any] = {"name": "start", "functions": []}

_PREP = _PreparedTools(
    flow_config={}, global_funcs=[], tool_retention=None, tool_projection=None
)


def _configurations(**overrides: Any) -> SimpleNamespace:
    base: Dict[str, Any] = dict(
        state_reducers=[],
        tool_arg_injection=[],
        client_context=None,
        ui_catalog=None,
        ui_intents=None,
        tool_execution=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_agent(configurations: Any = None) -> ChatAgent:
    template = TemplateModel.model_construct(
        id="tpl-1", name="t", flow={}, configurations=configurations
    )
    agent = ChatAgent(
        session_id="sess-1",
        template=template,
        llm=object(),
        template_vars={},
    )
    agent._turn_id = "turn-1"
    return agent


def _patch_db(monkeypatch) -> None:
    async def _noop(**_kwargs):
        return None

    _patch_agent_attr(monkeypatch, "insert_chat_message", _noop)
    _patch_agent_attr(monkeypatch, "update_chat_session_after_turn", _noop)
    _patch_agent_attr(monkeypatch, "upsert_agent_session_state_merge", _noop)


def _patch_stream(monkeypatch, cycles: List[List[tuple]]) -> None:
    calls = {"n": 0}

    async def _stream(_llm, _context, **_kwargs):
        script = cycles[calls["n"]]
        calls["n"] += 1
        for event in script:
            yield event

    monkeypatch.setattr(llm_driver, "stream", _stream)


def _tool_call(name: str, call_id: str) -> FunctionCallFromLLM:
    return FunctionCallFromLLM(
        function_name=name,
        tool_call_id=call_id,
        arguments={"query": "bras"},
        context=None,
    )


class _ConcurrencyProbe:
    """Dispatch stub that records the max number of overlapping calls."""

    def __init__(self, results: Dict[str, Any], delay: float = 0.02) -> None:
        self.results = results
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.completion_order: List[str] = []

    def patch(self, monkeypatch) -> None:
        probe = self

        async def _dispatch(self_agent, call, _node, _funcs, injected_args=None):
            probe.active += 1
            probe.max_active = max(probe.max_active, probe.active)
            try:
                await asyncio.sleep(probe.delay)
                return probe.results[call.tool_call_id], None
            finally:
                probe.active -= 1
                probe.completion_order.append(call.tool_call_id)

        monkeypatch.setattr(ChatAgent, "_dispatch_tool_call", _dispatch)


async def _run_cycle(
    monkeypatch,
    calls: List[FunctionCallFromLLM],
    probe: _ConcurrencyProbe,
    configurations: Any = None,
):
    _patch_stream(
        monkeypatch,
        [
            [("tool_call", c) for c in calls],
            [("text", "All done.")],
        ],
    )
    _patch_db(monkeypatch)
    probe.patch(monkeypatch)
    agent = _make_agent(configurations)
    context = LLMContext(messages=[{"role": "user", "content": "find bras"}])
    events = [ev async for ev in agent._cycle_loop(context, dict(_NODE), _PREP)]
    return events, context


# ---------------------------------------------------------------------------
# Annotation resolution
# ---------------------------------------------------------------------------


def test_annotation_precedence():
    # Flavor registry (loaded via the commerce import above).
    assert resolve_tool_annotation("search_catalog") == "read_only"
    assert resolve_tool_annotation("update_cart") == "idempotent"
    # Unknown tool → destructive (never accidentally parallel).
    assert resolve_tool_annotation("mystery_tool") == "destructive"
    # Template override beats the registry.
    template = SimpleNamespace(
        configurations=SimpleNamespace(
            tool_execution=SimpleNamespace(
                annotations={"search_catalog": "destructive", "my_read": "read_only"}
            )
        )
    )
    assert resolve_tool_annotation("search_catalog", template) == "destructive"
    assert resolve_tool_annotation("my_read", template) == "read_only"
    # Invalid override value falls through to the registry/default.
    bad = SimpleNamespace(
        configurations=SimpleNamespace(
            tool_execution=SimpleNamespace(annotations={"search_catalog": "fast"})
        )
    )
    assert resolve_tool_annotation("search_catalog", bad) == "read_only"


def test_conflicting_registration_raises():
    register_tool_annotations({"annot_probe": "read_only"})
    register_tool_annotations({"annot_probe": "read_only"})  # idempotent
    try:
        register_tool_annotations({"annot_probe": "destructive"})
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("conflicting annotation must raise")


# ---------------------------------------------------------------------------
# Parallel fan-out
# ---------------------------------------------------------------------------


async def test_all_read_only_batch_dispatches_concurrently(monkeypatch):
    calls = [_tool_call("search_catalog", "fc-1"), _tool_call("lookup_catalog", "fc-2")]
    probe = _ConcurrencyProbe(
        {
            "fc-1": {"products": [1, 2]},
            "fc-2": {"products": [3]},
        }
    )
    events, context = await _run_cycle(monkeypatch, calls, probe)

    assert probe.max_active == 2  # genuinely overlapped
    names = [ev.event for ev in events]
    assert names.count("step_started") == 2
    assert names.count("step_completed") == 2
    assert names.count("function_call_completed") == 2
    # Both step_started lines precede any completion (batch appears together).
    assert max(i for i, n in enumerate(names) if n == "step_started") < min(
        i for i, n in enumerate(names) if n == "step_completed"
    )

    # Tool results enter the LLM context in ORIGINAL call order.
    tool_msgs = [
        m
        for m in context.get_messages()
        if isinstance(m, dict) and m.get("role") == "tool"
    ]
    assert [m["tool_call_id"] for m in tool_msgs] == ["fc-1", "fc-2"]


async def test_mixed_batch_stays_sequential(monkeypatch):
    calls = [_tool_call("search_catalog", "fc-1"), _tool_call("update_cart", "fc-2")]
    probe = _ConcurrencyProbe(
        {
            "fc-1": {"products": []},
            # update_cart with free-form args (no cart.line_items) — the
            # commerce verifier no-ops on those.
            "fc-2": {"ok": True},
        }
    )
    events, _context = await _run_cycle(monkeypatch, calls, probe)
    assert probe.max_active == 1
    assert probe.completion_order == ["fc-1", "fc-2"]


async def test_template_kill_switch_disables_fanout(monkeypatch):
    calls = [_tool_call("search_catalog", "fc-1"), _tool_call("lookup_catalog", "fc-2")]
    probe = _ConcurrencyProbe({"fc-1": {"products": []}, "fc-2": {"products": []}})
    _events, _context = await _run_cycle(
        monkeypatch,
        calls,
        probe,
        configurations=_configurations(
            tool_execution=SimpleNamespace(parallel_read_only=False)
        ),
    )
    assert probe.max_active == 1


# ---------------------------------------------------------------------------
# Verification hooks
# ---------------------------------------------------------------------------


async def test_failed_verification_becomes_error_envelope(monkeypatch):
    # update_cart asked for qty 2 of a variant; the cart came back with 1.
    call = FunctionCallFromLLM(
        function_name="update_cart",
        tool_call_id="fc-1",
        arguments={"cart": {"line_items": [{"item": {"id": "v9"}, "quantity": 2}]}},
        context=None,
    )
    probe = _ConcurrencyProbe(
        {
            "fc-1": {
                "status": "success",
                "data": json.dumps(
                    {"line_items": [{"item": {"id": "v9"}, "quantity": 1}]}
                ),
            }
        }
    )
    events, context = await _run_cycle(monkeypatch, [call], probe)

    completed = next(ev for ev in events if ev.event == "step_completed")
    assert completed.data["status"] == "error"
    tool_msg = next(
        m
        for m in context.get_messages()
        if isinstance(m, dict) and m.get("role") == "tool"
    )
    body = json.loads(tool_msg["content"])
    assert body["status"] == "error"
    assert "verification failed" in body["error"]
    assert "requested quantity 2" in body["error"]
    # Original payload preserved for the model to reason over.
    assert "unverified_result" in body


async def test_satisfied_verification_passes_result_through(monkeypatch):
    call = FunctionCallFromLLM(
        function_name="update_cart",
        tool_call_id="fc-1",
        arguments={"cart": {"line_items": [{"item": {"id": "v9"}, "quantity": 2}]}},
        context=None,
    )
    payload = {
        "status": "success",
        "data": json.dumps({"line_items": [{"item": {"id": "v9"}, "quantity": 2}]}),
    }
    probe = _ConcurrencyProbe({"fc-1": payload})
    events, context = await _run_cycle(monkeypatch, [call], probe)
    completed = next(ev for ev in events if ev.event == "step_completed")
    assert completed.data["status"] == "ok"
    tool_msg = next(
        m
        for m in context.get_messages()
        if isinstance(m, dict) and m.get("role") == "tool"
    )
    assert json.loads(tool_msg["content"]) == payload


def test_raising_verifier_fails_open(monkeypatch):
    def _boom(_args, _result):
        raise RuntimeError("bad verifier")

    register_tool_verifier("failopen_tool", _boom)
    assert run_tool_verifiers("failopen_tool", {}, {"ok": True}) is None
    # Cleanup so other tests never see the probe verifier.
    verification_module._VERIFIERS.pop("failopen_tool", None)


def test_error_envelopes_skip_verification():
    # A tool that already failed is not re-judged (no double error).
    assert (
        run_tool_verifiers(
            "update_cart",
            {"cart": {"line_items": [{"item": {"id": "v9"}, "quantity": 2}]}},
            {"status": "error", "error": "upstream 500"},
        )
        is None
    )

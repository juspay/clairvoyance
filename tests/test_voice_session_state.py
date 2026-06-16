"""Voice SessionStatePolicy — back-port of inject_tool_args / apply_state_reducers
to the voice global-function wrapper.

Chat runs these in ChatAgent._cycle_loop; voice has no such loop, so the shared
``_make_global_wrapper`` applies them — gated by ``handles_state_externally`` so
chat (which sets the flag) is NOT double-applied. The pure inject/reduce engines
themselves are covered in tests/test_session_state.py; here we test the voice
wiring + the gating.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.template.global_function import (
    _make_global_wrapper,
)
from app.ai.voice.agents.breeze_buddy.template.session_state import (
    _inject_voice_state,
    _reduce_voice_state,
)

# template.types FIRST — warm up the template package before importing
# global_function (same import-order constraint as tests/test_session_state.py).
from app.ai.voice.agents.breeze_buddy.template.types import (  # isort: skip
    GlobalBuiltinFunction,
    StateReducer,
    ToolArgInjection,
)


def _flow_result(payload: dict, status: str = "success") -> dict:
    return {"status": status, "data": json.dumps(payload)}


def _func(name: str = "update_cart") -> GlobalBuiltinFunction:
    return GlobalBuiltinFunction.model_validate(
        {
            "type": "builtin",
            "name": name,
            "description": "Update the cart",
            "handler": "noop_handler",
        }
    )


def _configs() -> SimpleNamespace:
    return SimpleNamespace(
        tool_arg_injection=[
            ToolArgInjection(
                tool_name="update_cart", set_paths={"cart_id": "state.data.cart_id"}
            )
        ],
        state_reducers=[
            StateReducer(tool_name="update_cart", set_paths={"cart_id": "cart.id"})
        ],
    )


def _voice_bot(agent_state: Dict[str, Any]) -> SimpleNamespace:
    """Voice bot: NO handles_state_externally -> wrapper manages state."""
    return SimpleNamespace(
        configurations=_configs(),
        agent_state=dict(agent_state),
        _widget_resume_seed=None,
        lead=None,
        call_sid="cs1",
    )


def _recording_handler(record: Dict[str, Any], result: Any):
    async def handler(args, function_config=None):
        record["args"] = args
        return result

    return handler


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_inject_voice_state_fills_missing_from_state():
    out = _inject_voice_state(_voice_bot({"cart_id": "C1"}), "update_cart", {})
    assert out["cart_id"] == "C1"


def test_inject_voice_state_llm_value_wins_only_if_missing():
    out = _inject_voice_state(
        _voice_bot({"cart_id": "C1"}), "update_cart", {"cart_id": "FROM_LLM"}
    )
    assert out["cart_id"] == "FROM_LLM"


def test_inject_voice_state_noop_without_rules():
    bot = SimpleNamespace(
        configurations=SimpleNamespace(tool_arg_injection=[]),
        agent_state={"cart_id": "C1"},
    )
    assert _inject_voice_state(bot, "update_cart", {"x": 1}) == {"x": 1}


def test_inject_voice_state_noop_without_configurations():
    bot = SimpleNamespace(configurations=None, agent_state={"cart_id": "C1"})
    assert _inject_voice_state(bot, "update_cart", {"x": 1}) == {"x": 1}


def test_reduce_voice_state_lifts_identifier_into_agent_state():
    bot = _voice_bot({})
    _reduce_voice_state(bot, "update_cart", _flow_result({"cart": {"id": "C2"}}))
    assert bot.agent_state["cart_id"] == "C2"


def test_reduce_voice_state_noop_without_reducers():
    bot = SimpleNamespace(
        configurations=SimpleNamespace(state_reducers=[]), agent_state={"a": 1}
    )
    _reduce_voice_state(bot, "update_cart", _flow_result({"cart": {"id": "C2"}}))
    assert bot.agent_state == {"a": 1}


# ---------------------------------------------------------------------------
# wrapper gating (voice applies, chat skips)
# ---------------------------------------------------------------------------


async def test_wrapper_injects_and_reduces_on_voice():
    record: Dict[str, Any] = {}
    bot = _voice_bot({"cart_id": "C1"})
    wrapper = _make_global_wrapper(
        _func(), _recording_handler(record, _flow_result({"cart": {"id": "C2"}})), bot
    )
    # LLM provided no cart_id; the wrapper injects it from agent_state...
    result = await wrapper({}, None)
    assert record["args"].get("cart_id") == "C1"
    # ...and reduces the result back into agent_state.
    assert bot.agent_state["cart_id"] == "C2"
    assert result == _flow_result({"cart": {"id": "C2"}})


async def test_wrapper_skips_state_when_handled_externally():
    """Chat sets handles_state_externally -> wrapper neither injects nor reduces."""
    record: Dict[str, Any] = {}
    bot = _voice_bot({"cart_id": "C1"})
    bot.handles_state_externally = True
    wrapper = _make_global_wrapper(
        _func(), _recording_handler(record, _flow_result({"cart": {"id": "C2"}})), bot
    )
    await wrapper({}, None)
    assert "cart_id" not in record["args"]  # not injected by the wrapper
    assert bot.agent_state == {"cart_id": "C1"}  # not reduced by the wrapper

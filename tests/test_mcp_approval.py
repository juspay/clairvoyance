"""HITL approval for MCP tools.

Companion: [[mcp/__init__.py]] (_gate_mcp_handler, _mcp_approval_timeout_secs,
the loader approval-map wiring) and [[template/approval.py]] (gate_call).

Covers the channel-generic pieces without network/Redis by using the
declared-schemas (``tool_schemas``) loader path, which builds
FlowsFunctionSchema objects with no MCP handshake, plus direct unit tests of
the gate wrapper with a scripted ApprovalManager.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.ai.voice.agents.breeze_buddy import mcp as _mcp_module
from app.ai.voice.agents.breeze_buddy.mcp import (
    _gate_mcp_handler,
    _mcp_approval_timeout_secs,
    _state_wrap_mcp_handler,
    get_mcp_global_functions,
    get_mcp_global_functions_cached,
)
from app.ai.voice.agents.breeze_buddy.template.approval import ApprovalOutcome
from app.ai.voice.agents.breeze_buddy.template.types import (
    ApprovalConfig,
    McpConfig,
    McpServerConfig,
    StateReducer,
    ToolArgInjection,
)


@pytest.fixture(autouse=True)
def _bypass_ssrf_egress(monkeypatch):
    """These tests use placeholder MCP hostnames to exercise approval-map /
    tool-loading logic. SSRF egress validation (tested separately in
    tests/test_ssrf_egress.py) would otherwise reject the unresolvable host.
    """

    async def _ok(url, *args, **kwargs):
        return ["203.0.113.10"]

    monkeypatch.setattr(_mcp_module, "validate_egress_url", _ok)


class _FakeManager:
    """Scripted ApprovalManager stand-in (request() returns a fixed outcome)."""

    def __init__(self, outcome: ApprovalOutcome):
        self.outcome = outcome
        self.requests: List[Dict[str, Any]] = []

    async def request(self, function_name, arguments, config):
        self.requests.append(
            {"function_name": function_name, "arguments": arguments, "config": config}
        )
        return self.outcome


def _voice_bot(outcome: ApprovalOutcome) -> SimpleNamespace:
    """Daily voice bot: has an ApprovalManager, gates in-process."""
    return SimpleNamespace(
        handles_approval_externally=False,
        is_daily_mode=True,
        approval_manager=_FakeManager(outcome),
    )


def _approval(**overrides) -> ApprovalConfig:
    cfg: Dict[str, Any] = {"prompt": "Approve checkout?", "timeout_secs": 90}
    cfg.update(overrides)
    return ApprovalConfig.model_validate(cfg)


def _server(
    tool_approvals: Optional[Dict[str, Any]] = None,
    *,
    name: str = "shopify",
    tool_name: str = "create_checkout",
    extra_tool: Optional[str] = None,
) -> McpServerConfig:
    schemas = [
        {"name": tool_name, "description": "Create a checkout", "properties": {}}
    ]
    if extra_tool:
        schemas.append(
            {"name": extra_tool, "description": "Search the catalog", "properties": {}}
        )
    return McpServerConfig.model_validate(
        {
            "enabled": True,
            "name": name,
            "url": "https://shop.example/api/mcp",
            "auth": {"type": "none"},
            "tool_schemas": schemas,
            "tool_approvals": tool_approvals or {},
        }
    )


# ---------------------------------------------------------------------------
# model + helpers
# ---------------------------------------------------------------------------


def test_tool_approvals_parses_into_approval_config():
    server = _server({"create_checkout": {"prompt": "ok?", "on_no_channel": "deny"}})
    cfg = server.tool_approvals["create_checkout"]
    assert isinstance(cfg, ApprovalConfig)
    assert cfg.prompt == "ok?"
    assert cfg.on_no_channel.value == "deny"
    assert _server().tool_approvals == {}


def test_watchdog_budget_is_additive():
    # approval wait (90) + MCP exec ceiling (60) + margin (15)
    assert _mcp_approval_timeout_secs(_approval(timeout_secs=90)) == 165.0
    assert _mcp_approval_timeout_secs(_approval(timeout_secs=10)) == 85.0


# ---------------------------------------------------------------------------
# _gate_mcp_handler — voice in-closure gate
# ---------------------------------------------------------------------------


async def _recording_handler(record: List[str], result: Any):
    async def handler(args: Dict[str, Any], flow_manager: Any) -> Any:
        record.append("executed")
        return result

    return handler


async def test_gate_passthrough_when_no_approval():
    record: List[str] = []
    inner = await _recording_handler(record, {"status": "success", "data": "x"})
    wrapped = _gate_mcp_handler(
        inner, _voice_bot(ApprovalOutcome(True, "approved")), "t", None
    )
    assert wrapped is inner  # identity — no wrap


async def test_gate_passthrough_when_no_bot():
    record: List[str] = []
    inner = await _recording_handler(record, {"status": "success", "data": "x"})
    wrapped = _gate_mcp_handler(inner, None, "t", _approval())
    assert wrapped is inner


async def test_gate_approve_runs_real_handler():
    record: List[str] = []
    inner = await _recording_handler(record, {"status": "success", "data": "done"})
    bot = _voice_bot(ApprovalOutcome(True, "approved"))
    wrapped = _gate_mcp_handler(inner, bot, "create_checkout", _approval())
    result = await wrapped({"line_items": [1]}, None)
    assert record == ["executed"]
    assert result == {"status": "success", "data": "done"}
    # the LLM-visible name + args reach the manager (the approval card)
    assert bot.approval_manager.requests[0]["function_name"] == "create_checkout"
    assert bot.approval_manager.requests[0]["arguments"] == {"line_items": [1]}


async def test_gate_deny_skips_real_handler():
    record: List[str] = []
    inner = await _recording_handler(record, {"status": "success", "data": "done"})
    bot = _voice_bot(ApprovalOutcome(False, "denied", reason="user said no"))
    wrapped = _gate_mcp_handler(inner, bot, "create_checkout", _approval())
    result = await wrapped({"x": 1}, None)
    assert record == []  # real MCP round-trip never ran — no side effect
    assert result == {"status": "denied", "reason": "user said no"}


async def test_gate_timeout_maps_to_timeout_status():
    record: List[str] = []
    inner = await _recording_handler(record, {"status": "success", "data": "done"})
    bot = _voice_bot(ApprovalOutcome(False, "timeout", reason="no decision in time"))
    wrapped = _gate_mcp_handler(inner, bot, "t", _approval())
    result = await wrapped({}, None)
    assert record == []
    assert result["status"] == "timeout"


async def test_gate_superseded_maps_to_not_decided():
    record: List[str] = []
    inner = await _recording_handler(record, {"status": "success", "data": "done"})
    bot = _voice_bot(ApprovalOutcome(False, "superseded"))
    wrapped = _gate_mcp_handler(inner, bot, "t", _approval())
    result = await wrapped({}, None)
    assert result["status"] == "not_decided"  # not a refusal


async def test_gate_chat_bot_executes_without_manager():
    """Chat sets handles_approval_externally — the in-closure gate is a no-op."""
    record: List[str] = []
    inner = await _recording_handler(record, {"status": "success", "data": "done"})
    bot = SimpleNamespace(handles_approval_externally=True, approval_manager=None)
    wrapped = _gate_mcp_handler(inner, bot, "t", _approval())
    result = await wrapped({}, None)
    assert record == ["executed"]
    assert result == {"status": "success", "data": "done"}


async def test_gate_daily_without_manager_denies():
    """Daily mode but no approval channel (RTVI off) must DENY, never execute."""
    record: List[str] = []
    inner = await _recording_handler(record, {"status": "success", "data": "done"})
    bot = SimpleNamespace(
        handles_approval_externally=False, is_daily_mode=True, approval_manager=None
    )
    wrapped = _gate_mcp_handler(inner, bot, "t", _approval())
    result = await wrapped({}, None)
    assert record == []
    assert result == {"status": "denied", "reason": "approval_channel_unavailable"}


async def test_gate_telephony_on_no_channel_deny():
    record: List[str] = []
    inner = await _recording_handler(record, {"status": "success", "data": "done"})
    bot = SimpleNamespace(
        handles_approval_externally=False, is_daily_mode=False, approval_manager=None
    )
    wrapped = _gate_mcp_handler(inner, bot, "t", _approval(on_no_channel="deny"))
    result = await wrapped({}, None)
    assert record == []
    assert result["status"] == "denied"


async def test_gate_telephony_on_no_channel_execute():
    record: List[str] = []
    inner = await _recording_handler(record, {"status": "success", "data": "done"})
    bot = SimpleNamespace(
        handles_approval_externally=False, is_daily_mode=False, approval_manager=None
    )
    wrapped = _gate_mcp_handler(inner, bot, "t", _approval(on_no_channel="execute"))
    await wrapped({}, None)
    assert record == ["executed"]  # telephony fallback executes (loud warning)


# ---------------------------------------------------------------------------
# chat loader — get_mcp_global_functions_cached returns (functions, map)
# ---------------------------------------------------------------------------


async def test_chat_loader_returns_approval_map_keyed_by_registered_name():
    cfg = McpConfig(servers=[_server({"create_checkout": {"prompt": "ok?"}})])
    funcs, approvals = await get_mcp_global_functions_cached(cfg, {}, "tmpl-1")
    assert {f.name for f in funcs} == {"create_checkout"}
    assert set(approvals) == {"create_checkout"}
    assert isinstance(approvals["create_checkout"], ApprovalConfig)


async def test_chat_loader_ungated_tool_absent_from_map():
    cfg = McpConfig(servers=[_server(extra_tool="search_catalog")])  # no approvals
    funcs, approvals = await get_mcp_global_functions_cached(cfg, {}, "tmpl-1")
    assert {f.name for f in funcs} == {"create_checkout", "search_catalog"}
    assert approvals == {}


async def test_chat_loader_collision_keys_under_prefixed_name():
    """A gated tool colliding across servers is keyed under <server>_<name>."""
    cfg = McpConfig(
        servers=[
            _server({"create_checkout": {"prompt": "A"}}, name="main"),
            _server({"create_checkout": {"prompt": "B"}}, name="alt"),
        ]
    )
    funcs, approvals = await get_mcp_global_functions_cached(cfg, {}, "tmpl-1")
    names = {f.name for f in funcs}
    assert names == {"create_checkout", "alt_create_checkout"}
    # both gated, each under its REGISTERED name (the name the LLM calls)
    assert set(approvals) == {"create_checkout", "alt_create_checkout"}
    assert approvals["create_checkout"].prompt == "A"
    assert approvals["alt_create_checkout"].prompt == "B"


# ---------------------------------------------------------------------------
# voice loader — get_mcp_global_functions wraps + sizes the watchdog
# ---------------------------------------------------------------------------


async def test_voice_loader_sets_watchdog_budget_on_gated_tool_only():
    cfg = McpConfig(
        servers=[
            _server(
                {"create_checkout": {"timeout_secs": 90}}, extra_tool="search_catalog"
            )
        ]
    )
    funcs = await get_mcp_global_functions(
        cfg, {}, bot_instance=_voice_bot(ApprovalOutcome(True, "approved"))
    )
    by_name = {f.name: f for f in funcs}
    assert by_name["create_checkout"].timeout_secs == 165.0  # 90 + 60 + 15
    # ungated tool keeps the FlowsFunctionSchema default (no additive budget)
    assert by_name["search_catalog"].timeout_secs is None


async def test_voice_loader_gated_handler_denies_without_network():
    """A denied gated MCP tool returns a denial and never makes the HTTP call."""
    cfg = McpConfig(servers=[_server({"create_checkout": {"prompt": "ok?"}})])
    bot = _voice_bot(ApprovalOutcome(False, "denied", reason="nope"))
    funcs = await get_mcp_global_functions(cfg, {}, bot_instance=bot)
    # handler uses the legacy (args, flow_manager) MCP convention; type as Any
    # so pyrefly doesn't apply FlowsFunctionSchema's single-arg handler type.
    handler: Any = {f.name: f.handler for f in funcs}["create_checkout"]
    # Deny path short-circuits before the real httpx POST — no network needed.
    result = await handler({"line_items": [1]}, None)
    assert result == {"status": "denied", "reason": "nope"}
    assert bot.approval_manager.requests[0]["function_name"] == "create_checkout"


# ---------------------------------------------------------------------------
# voice SessionStatePolicy for MCP tools — _state_wrap_mcp_handler
# (inject_tool_args / apply_state_reducers, the MCP counterpart of
# _make_global_wrapper's state hook; the pure engines are covered in
# tests/test_session_state.py)
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402


def _state_bot(
    agent_state: Dict[str, Any], *, external: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(
        handles_state_externally=external,
        configurations=SimpleNamespace(
            tool_arg_injection=[
                ToolArgInjection(
                    tool_name="create_checkout",
                    set_paths={"cart_id": "state.data.cart_id"},
                )
            ],
            state_reducers=[
                StateReducer(
                    tool_name="create_checkout", set_paths={"cart_id": "cart.id"}
                )
            ],
        ),
        agent_state=dict(agent_state),
        _widget_resume_seed=None,
        lead=None,
        call_sid="cs1",
    )


def _mcp_envelope(payload: dict) -> dict:
    return {"status": "success", "data": _json.dumps(payload)}


def _recording_mcp_handler(record: Dict[str, Any], result: Any):
    async def handler(args, flow_manager):
        record["args"] = args
        return result

    return handler


async def test_state_wrap_injects_and_reduces_on_voice():
    record: Dict[str, Any] = {}
    bot = _state_bot({"cart_id": "C1"})
    wrapped = _state_wrap_mcp_handler(
        _recording_mcp_handler(record, _mcp_envelope({"cart": {"id": "C2"}})),
        bot,
        "create_checkout",
    )
    result = await wrapped({}, None)
    assert record["args"].get("cart_id") == "C1"  # injected from state
    assert bot.agent_state["cart_id"] == "C2"  # reduced from the result
    assert result == _mcp_envelope({"cart": {"id": "C2"}})


async def test_state_wrap_skips_when_handled_externally():
    record: Dict[str, Any] = {}
    bot = _state_bot({"cart_id": "C1"}, external=True)
    wrapped = _state_wrap_mcp_handler(
        _recording_mcp_handler(record, _mcp_envelope({"cart": {"id": "C2"}})),
        bot,
        "create_checkout",
    )
    await wrapped({}, None)
    assert "cart_id" not in record["args"]  # not injected by the wrapper
    assert bot.agent_state == {"cart_id": "C1"}  # not reduced by the wrapper


async def test_state_wrap_noop_without_bot():
    handler = _recording_mcp_handler({}, _mcp_envelope({}))
    assert _state_wrap_mcp_handler(handler, None, "create_checkout") is handler

"""Tests for the centralized tool-call result pipeline
(``handlers/transport/utils/tool_pipeline.py``) shared by MCP tools and
Global HTTP functions: projection → transforms → ui-hint, the MCP-shaped
JSON-string entrypoint's no-op contract, and the MCP direct-HTTP handler
wiring (tool_response_schemas projection + isError gating)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from mcp.client.session_group import StreamableHttpParameters

from app.ai.voice.agents.breeze_buddy import mcp as mcp_mod
from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.response_transform import (
    TRANSFORM_REGISTRY,
)
from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.tool_pipeline import (
    apply_result_pipeline,
    apply_result_pipeline_json_str,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ResponseTransform,
    ToolUiHint,
    ToolUiTrigger,
)

# --- test-only transforms -------------------------------------------------
# Registered per-test and restored on teardown so the process-global
# TRANSFORM_REGISTRY is never polluted for other test modules.


def _append_z(value, args):
    # value is the list at the matched path; return a NEW list so we can prove
    # the pipeline deep-copies (the caller's original list must stay intact).
    return list(value) + ["z"]


def _boom(value, args):
    raise ValueError("boom")


@pytest.fixture(autouse=True)
def _bypass_ssrf_egress(monkeypatch):
    """These tests drive the response pipeline through a placeholder MCP host.

    The direct-HTTP handler now revalidates egress at call time, which would
    reject `http://x/mcp` before any of the projection/transform behaviour under
    test here runs. The guard itself is covered in tests/test_ssrf_egress.py,
    including a case asserting this handler refuses a rebinding host.
    """

    async def _ok(url, *args, **kwargs):
        return ["203.0.113.10"]

    monkeypatch.setattr(mcp_mod, "validate_egress_url", _ok)


@pytest.fixture(autouse=True)
def _register_test_transforms():
    added = {"test_append_z": _append_z, "test_boom": _boom}
    saved = {k: TRANSFORM_REGISTRY[k] for k in added if k in TRANSFORM_REGISTRY}
    TRANSFORM_REGISTRY.update(added)
    try:
        yield
    finally:
        for k in added:
            if k in saved:
                TRANSFORM_REGISTRY[k] = saved[k]
            else:
                TRANSFORM_REGISTRY.pop(k, None)


# --- projection -----------------------------------------------------------


def test_projection_narrows_to_whitelist():
    data = {"a": {"b": 1}, "c": 2}
    out = apply_result_pipeline(data, response_schema={"kept": "a.b"})
    assert out == {"kept": 1}


def test_projection_resolves_placeholder_from_args():
    data = {"items": [{"id": "a", "v": 1}, {"id": "b", "v": 2}]}
    out = apply_result_pipeline(
        data,
        response_schema={"match": "items[?id==`{wanted}`]"},
        args={"wanted": "a"},
    )
    assert out == {"match": [{"id": "a", "v": 1}]}


def test_projection_skipped_on_error():
    data = {"error": "not found", "detail": "x"}
    out = apply_result_pipeline(data, is_success=False, response_schema={"k": "error"})
    assert out == data  # error bodies pass through unfiltered


def test_projection_full_sentinel_is_passthrough():
    data = {"a": 1, "b": 2}
    out = apply_result_pipeline(data, response_schema="full")
    assert out == data


# --- transforms -----------------------------------------------------------


def test_transforms_deepcopy_isolates_caller_input():
    data = {"items": ["a", "b"]}
    t = ResponseTransform(path="items", fn="test_append_z", args={})
    out = apply_result_pipeline(data, response_transforms=[t])
    assert out["items"] == ["a", "b", "z"]
    assert data["items"] == ["a", "b"]  # original untouched — proves deep copy


def test_transforms_failure_is_swallowed_and_returns_input():
    data = {"n": 1}
    t = ResponseTransform(path="n", fn="test_boom", args={})
    out = apply_result_pipeline(data, response_transforms=[t])
    assert out == {"n": 1}


def test_transforms_skipped_on_error():
    data = {"items": ["a"]}
    t = ResponseTransform(path="items", fn="test_append_z", args={})
    out = apply_result_pipeline(data, is_success=False, response_transforms=[t])
    assert out == {"items": ["a"]}


# --- ui-hint trigger matrix ----------------------------------------------


def test_ui_hint_on_success_injects():
    hint = ToolUiHint(trigger=ToolUiTrigger.ON_SUCCESS, instructions="render")
    out = apply_result_pipeline({"x": 1}, is_success=True, ui_hint=hint)
    assert out["_ui_instructions"] == "render"


def test_ui_hint_on_success_skipped_on_error():
    hint = ToolUiHint(trigger=ToolUiTrigger.ON_SUCCESS, instructions="render")
    out = apply_result_pipeline({"x": 1}, is_success=False, ui_hint=hint)
    assert "_ui_instructions" not in out


def test_ui_hint_on_any_injects_even_on_error():
    hint = ToolUiHint(trigger=ToolUiTrigger.ON_ANY, instructions="render")
    out = apply_result_pipeline({"x": 1}, is_success=False, ui_hint=hint)
    assert out["_ui_instructions"] == "render"


def test_ui_hint_skip_ui_sets_flag_even_on_error():
    hint = ToolUiHint(trigger=ToolUiTrigger.SKIP_UI, instructions="(unused)")
    out = apply_result_pipeline({"x": 1}, is_success=False, ui_hint=hint)
    assert out["_ui_skip"] is True
    assert "_ui_instructions" not in out


def test_ui_hint_dict_only_lists_pass_through():
    hint = ToolUiHint(trigger=ToolUiTrigger.ON_ANY, instructions="render")
    out = apply_result_pipeline([{"id": 1}], ui_hint=hint)
    assert out == [{"id": 1}]


# --- full pipeline order --------------------------------------------------


def test_full_pipeline_projection_then_transforms_then_ui_hint():
    data = {"items": ["a", "b"], "noise": "drop me"}
    # 1. projection keeps only ``items`` (as ``items``)
    # 2. transform appends "z" to the projected list
    # 3. ui-hint splices _ui_instructions onto the resulting dict
    t = ResponseTransform(path="items", fn="test_append_z", args={})
    hint = ToolUiHint(trigger=ToolUiTrigger.ON_SUCCESS, instructions="render")
    out = apply_result_pipeline(
        data,
        response_schema={"items": "items"},
        response_transforms=[t],
        ui_hint=hint,
    )
    assert out["items"] == ["a", "b", "z"]
    assert "noise" not in out
    assert out["_ui_instructions"] == "render"


# --- JSON-string entrypoint (MCP-shaped) ----------------------------------


def test_json_str_noop_when_no_config_returns_exact_input():
    result = json.dumps({"products": [1, 2]})
    assert apply_result_pipeline_json_str(result) == result


def test_json_str_noop_on_plain_text():
    plain = "Sorry, no results."
    hint = ToolUiHint(trigger=ToolUiTrigger.ON_SUCCESS, instructions="x")
    assert apply_result_pipeline_json_str(plain, ui_hint=hint) == plain


def test_json_str_noop_on_non_string_input():
    payload = {"already": "parsed"}
    assert apply_result_pipeline_json_str(payload) is payload


def test_json_str_full_sentinel_alone_is_noop():
    result = json.dumps({"a": 1})
    assert apply_result_pipeline_json_str(result, response_schema="full") == result


def test_json_str_applies_projection_and_reencodes():
    result = json.dumps({"a": {"b": 1}, "c": 2})
    out = apply_result_pipeline_json_str(result, response_schema={"kept": "a.b"})
    assert json.loads(out) == {"kept": 1}


# --- MCP direct-HTTP handler wiring (tool_response_schemas + isError) --------
#
# These drive _create_direct_http_tool_handler end-to-end with a mocked
# JSON-RPC transport, covering the handler call sites the isolation tests
# above don't: that tool_response_schemas actually projects, that a tool-level
# error (isError) is NOT projected, and that a malformed schema fails open
# instead of escaping into the (unguarded) voice FlowManager.


def _jsonrpc_tool_response(payload: dict, *, is_error: bool = False) -> dict:
    """A JSON-RPC tools/call success envelope carrying `payload` as the tool's
    text content, optionally flagged as a tool-level error via ``isError``."""
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": is_error,
        },
    }


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, resp: _FakeResp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        return self._resp


def _server_params():
    return StreamableHttpParameters(
        url="http://x/mcp", headers={}, timeout=timedelta(seconds=5)
    )


def _patch_httpx(monkeypatch, resp: _FakeResp):
    monkeypatch.setattr(
        mcp_mod.httpx, "AsyncClient", lambda **kwargs: _FakeAsyncClient(resp)
    )


async def test_direct_handler_projects_on_success(monkeypatch):
    resp = _FakeResp(
        200, _jsonrpc_tool_response({"order": {"status": "shipped"}, "noise": 1})
    )
    _patch_httpx(monkeypatch, resp)
    handler = mcp_mod._create_direct_http_tool_handler(
        _server_params(),
        "get_order",
        response_schema={"order_status": "order.status"},
    )
    out = await handler({"id": "o1"}, None)
    assert out["status"] == "success"
    assert json.loads(out["data"]) == {"order_status": "shipped"}


async def test_direct_handler_does_not_project_tool_error(monkeypatch):
    # isError:true -> is_success=False -> projection skipped -> error survives,
    # instead of being stripped to {} by the success-shaped whitelist.
    resp = _FakeResp(
        200, _jsonrpc_tool_response({"error": "SKU not found"}, is_error=True)
    )
    _patch_httpx(monkeypatch, resp)
    handler = mcp_mod._create_direct_http_tool_handler(
        _server_params(),
        "get_order",
        response_schema={"order_status": "order.status"},
    )
    out = await handler({"id": "o1"}, None)
    assert out["status"] == "success"
    assert json.loads(out["data"]) == {"error": "SKU not found"}


async def test_direct_handler_bad_schema_fails_open(monkeypatch):
    # A malformed projection must NOT escape the handler (this call site has no
    # outer try/except); the fail-open pipeline logs and passes through raw.
    resp = _FakeResp(200, _jsonrpc_tool_response({"a": 1}))
    _patch_httpx(monkeypatch, resp)
    handler = mcp_mod._create_direct_http_tool_handler(
        _server_params(),
        "t",
        response_schema={"x": "a[?bad"},  # invalid JMESPath
    )
    out = await handler({}, None)  # must not raise
    assert out["status"] == "success"
    assert json.loads(out["data"]) == {"a": 1}

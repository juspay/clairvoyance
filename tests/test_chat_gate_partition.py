"""Chat HITL partition — node-aware gating (_partition_gated_calls).

A gated *global* function shadowed by a same-named *per-node* function must
NOT be gated in the node that defines the per-node one (the LLM calls the
per-node function there, which the author did not gate) — matching voice,
whose wrapper gates only globals. In nodes that don't shadow it, the gated
global is still gated.

The node's ``functions`` at runtime are ``FlowsFunctionSchema`` objects (the
builder runs every per-node function through ``_build_function_schema``), NOT
plain dicts — so these fixtures construct real schemas. Feeding plain dicts
here would exercise a shape that never reaches the partition in production.
"""

from __future__ import annotations

from types import SimpleNamespace

from pipecat_flows import FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.chat.agent import _partition_gated_calls


def _call(name: str) -> SimpleNamespace:
    return SimpleNamespace(function_name=name)


def _schema(name: str) -> FlowsFunctionSchema:
    """A per-node function as it actually appears in a built node."""
    return FlowsFunctionSchema(name=name, description="", properties={}, required=[])


def test_partition_gates_global_in_non_shadow_node():
    calls = [_call("issue_refund"), _call("search")]
    approval_map = {"issue_refund": object()}
    node = {"functions": [_schema("search")]}  # no per-node issue_refund
    gated, ungated = _partition_gated_calls(calls, approval_map, node)
    assert [c.function_name for c in gated] == ["issue_refund"]
    assert [c.function_name for c in ungated] == ["search"]


def test_partition_skips_gated_name_shadowed_by_per_node_function():
    calls = [_call("issue_refund")]
    approval_map = {"issue_refund": object()}
    node = {"functions": [_schema("issue_refund")]}  # per-node shadows it
    gated, ungated = _partition_gated_calls(calls, approval_map, node)
    assert gated == []  # not gated — the per-node fn runs (matches voice)
    assert [c.function_name for c in ungated] == ["issue_refund"]


def test_partition_ignores_non_schema_node_entries():
    # Only FlowsFunctionSchema entries can shadow (that is all
    # _dispatch_tool_call dispatches per-node). A stray non-schema entry —
    # e.g. a raw dict that slipped through — must NOT register as a per-node
    # function, so a gated global of that name is still gated.
    calls = [_call("issue_refund")]
    approval_map = {"issue_refund": object()}
    node = {"functions": [{"name": "issue_refund"}]}  # plain dict, not a schema
    gated, ungated = _partition_gated_calls(calls, approval_map, node)
    assert [c.function_name for c in gated] == ["issue_refund"]
    assert ungated == []


def test_partition_empty_or_missing_node_functions_gates_normally():
    calls = [_call("X")]
    approval_map = {"X": object()}
    for node in ({}, {"functions": []}, {"functions": None}):
        gated, ungated = _partition_gated_calls(calls, approval_map, node)
        assert [c.function_name for c in gated] == ["X"]
        assert ungated == []


def test_partition_ungated_call_unaffected():
    calls = [_call("search")]
    gated, ungated = _partition_gated_calls(calls, {"issue_refund": object()}, {})
    assert gated == []
    assert [c.function_name for c in ungated] == ["search"]

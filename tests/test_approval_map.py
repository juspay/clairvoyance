"""build_approval_map — discovery parity with GlobalFunctionRegistry.

Covers: flow mode (global_functions), direct mode (flat functions with the
function_name alias), malformed configs (skipped, never raise), unsupported
per-node placement (ignored), and name shadowing (still mapped — chat gates
by name; the builder logs the divergence warning).
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.template.approval import build_approval_map


def test_flow_mode_global_functions():
    flow = {
        "global_functions": [
            {
                "type": "http",
                "name": "issue_refund",
                "description": "d",
                "approval": {"prompt": "Refund OK?", "timeout_secs": 60},
            },
            {"type": "http", "name": "check_status", "description": "d"},
        ]
    }
    amap = build_approval_map(flow)
    assert set(amap) == {"issue_refund"}
    assert amap["issue_refund"].prompt == "Refund OK?"
    assert amap["issue_refund"].timeout_secs == 60


def test_direct_mode_function_name_alias():
    flow = {
        "mode": "direct",
        "functions": [
            {
                "type": "builtin",
                "function_name": "transfer_money",
                "description": "d",
                "handler": "h",
                "approval": {},
            },
            # Non-global types are FlowFunctions — never gated even if a
            # stray approval key is present.
            {
                "function_name": "collect_feedback",
                "description": "d",
                "approval": {},
            },
        ],
    }
    amap = build_approval_map(flow)
    assert set(amap) == {"transfer_money"}
    assert amap["transfer_money"].timeout_secs == 120  # defaults


def test_malformed_approval_config_is_skipped():
    flow = {
        "global_functions": [
            {
                "type": "http",
                "name": "bad",
                "description": "d",
                "approval": {"timeout_secs": "not-a-number"},
            },
            {
                "type": "http",
                "name": "good",
                "description": "d",
                "approval": {},
            },
        ]
    }
    amap = build_approval_map(flow)
    assert set(amap) == {"good"}


def test_per_node_approval_is_ignored():
    flow = {
        "global_functions": [],
        "nodes": [
            {
                "node_name": "start",
                "functions": [{"name": "node_fn", "description": "d", "approval": {}}],
            }
        ],
    }
    assert build_approval_map(flow) == {}


def test_shadowed_global_still_mapped():
    flow = {
        "global_functions": [
            {
                "type": "http",
                "name": "shared_name",
                "description": "d",
                "approval": {},
            }
        ],
        "nodes": [
            {
                "node_name": "start",
                "functions": [{"name": "shared_name", "description": "d"}],
            }
        ],
    }
    amap = build_approval_map(flow)
    assert "shared_name" in amap


def test_non_dict_flow_is_safe():
    assert build_approval_map(None) == {}
    assert build_approval_map([]) == {}
    assert build_approval_map({}) == {}


def test_global_function_types_match_enum():
    # The gated-type set must stay derived from GlobalFunctionType so chat
    # (build_approval_map) and voice/builder gate the same set. A hardcoded
    # literal would silently skip any newly added type on the chat side.
    from app.ai.voice.agents.breeze_buddy.template.approval import (
        _GLOBAL_FUNCTION_TYPES,
    )
    from app.ai.voice.agents.breeze_buddy.template.types import GlobalFunctionType

    assert _GLOBAL_FUNCTION_TYPES == {t.value for t in GlobalFunctionType}


def test_every_global_function_type_is_gateable():
    # Every enum-listed type, when carrying an approval config in flow mode,
    # is mapped — guards against a future type being added to the enum but not
    # gated by chat.
    from app.ai.voice.agents.breeze_buddy.template.types import GlobalFunctionType

    flow = {
        "global_functions": [
            {
                "type": t.value,
                "name": f"fn_{t.value}",
                "description": "d",
                "approval": {},
            }
            for t in GlobalFunctionType
        ]
    }
    amap = build_approval_map(flow)
    assert set(amap) == {f"fn_{t.value}" for t in GlobalFunctionType}

"""Unit tests for the MCP handler's _deep_merge_defaults helper.

The helper is used to merge protocol-level static arguments (e.g. Shopify
UCP's `meta.ucp-agent.profile`) into every tool call's `arguments` object
without touching session-state-driven injection rules.
"""

from app.ai.voice.agents.breeze_buddy.mcp import _deep_merge_defaults


def test_empty_defaults_returns_copy_of_args() -> None:
    args = {"a": 1, "b": 2}
    out = _deep_merge_defaults(args, {})
    assert out == args
    assert out is not args  # new dict


def test_defaults_fill_missing_keys() -> None:
    out = _deep_merge_defaults({"a": 1}, {"b": 2})
    assert out == {"a": 1, "b": 2}


def test_caller_wins_on_top_level_collision() -> None:
    out = _deep_merge_defaults({"a": 1}, {"a": 99})
    assert out == {"a": 1}


def test_deep_merge_of_nested_dicts() -> None:
    out = _deep_merge_defaults(
        {"catalog": {"query": "water"}},
        {
            "meta": {"ucp-agent": {"profile": "https://x/agent.json"}},
            "catalog": {"limit": 10},
        },
    )
    assert out == {
        "catalog": {"query": "water", "limit": 10},
        "meta": {"ucp-agent": {"profile": "https://x/agent.json"}},
    }


def test_caller_wins_on_nested_collision() -> None:
    out = _deep_merge_defaults(
        {"meta": {"ucp-agent": {"profile": "OVERRIDE"}}},
        {"meta": {"ucp-agent": {"profile": "https://default/agent.json"}}},
    )
    assert out == {"meta": {"ucp-agent": {"profile": "OVERRIDE"}}}


def test_ucp_profile_injection_idiomatic_shape() -> None:
    """The exact wire shape we send to Shopify UCP."""
    args_from_llm = {"catalog": {"query": "blue water bottle"}}
    server_defaults = {
        "meta": {
            "ucp-agent": {
                "profile": "https://breezebuddy.ai/.well-known/ucp/agent.json"
            }
        }
    }
    out = _deep_merge_defaults(args_from_llm, server_defaults)
    assert out["catalog"] == {"query": "blue water bottle"}
    assert (
        out["meta"]["ucp-agent"]["profile"]
        == "https://breezebuddy.ai/.well-known/ucp/agent.json"
    )


def test_args_with_partial_meta_still_get_profile() -> None:
    """If the LLM happens to pass its own `meta` for some reason, our profile
    should still land (defaults fill the missing ucp-agent subtree)."""
    out = _deep_merge_defaults(
        {"meta": {"other_namespace": {"x": 1}}},
        {"meta": {"ucp-agent": {"profile": "https://x/agent.json"}}},
    )
    assert out["meta"]["other_namespace"] == {"x": 1}
    assert out["meta"]["ucp-agent"]["profile"] == "https://x/agent.json"


def test_does_not_mutate_inputs() -> None:
    args = {"a": 1}
    defaults = {"b": 2}
    _ = _deep_merge_defaults(args, defaults)
    assert args == {"a": 1}
    assert defaults == {"b": 2}

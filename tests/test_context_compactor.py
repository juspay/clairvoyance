# pyrefly: ignore-errors
# Test indexes into result dicts whose return type pyrefly can't infer tightly.
"""Tests for the conversation-context compactor.

The chat compactor rewrites stale ``tool_result`` blocks in an Anthropic
``messages`` array to 1-line stubs. The voice compactor does the same for the
``role:"developer"`` ``async_tool`` envelope pipecat's assistant aggregator
stores for MCP tools. The contract is narrow but easy to get wrong on edge
cases (mixed roles, missing tool_call linkage), so the cases here cover both
the happy paths and the don't-blow-up paths.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.chat.history.compactor import (
    compact_tool_results,
)


def _tool_use(tu_id: str, name: str, args: dict) -> dict:
    return {"type": "tool_use", "id": tu_id, "name": name, "input": args}


def _tool_result(tu_id: str, content: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tu_id, "content": content}


def _assistant_with_tool_use(tu_id: str, name: str, args: dict) -> dict:
    return {"role": "assistant", "content": [_tool_use(tu_id, name, args)]}


def _user_with_tool_result(tu_id: str, content: str) -> dict:
    return {"role": "user", "content": [_tool_result(tu_id, content)]}


# ---------------------------------------------------------------------------
# Happy path — old catalog calls compact, latest stays intact
# ---------------------------------------------------------------------------


def test_compacts_older_last_turn_only_results_keeping_most_recent():
    messages = [
        {"role": "user", "content": "find me bottles"},
        _assistant_with_tool_use("u1", "search_catalog", {"query": "bottle"}),
        _user_with_tool_result("u1", '{"products": ["A","B","C"]}' * 200),  # bulky
        {"role": "assistant", "content": "Here are some bottles."},
        {"role": "user", "content": "red ones?"},
        _assistant_with_tool_use("u2", "search_catalog", {"query": "red bottle"}),
        _user_with_tool_result("u2", '{"products": ["red1"]}' * 200),  # bulky, NEWEST
    ]
    out = compact_tool_results(
        messages, retention={"search_catalog": "last_turn_only"}, recent_keep=1
    )
    # Older tool_result (u1) gets stubbed
    assert "[pruned: search_catalog" in out[2]["content"][0]["content"]
    assert "u1" == out[2]["content"][0]["tool_use_id"]
    # Newest tool_result (u2) untouched
    assert out[6]["content"][0]["content"].startswith('{"products"')
    # tool_use blocks are PRESERVED (LLM still sees what was called)
    assert out[1]["content"][0]["type"] == "tool_use"
    assert out[1]["content"][0]["name"] == "search_catalog"


def test_recent_keep_two_preserves_last_two_results():
    messages = [
        _assistant_with_tool_use("u1", "search_catalog", {"q": "1"}),
        _user_with_tool_result("u1", "first" * 200),
        _assistant_with_tool_use("u2", "search_catalog", {"q": "2"}),
        _user_with_tool_result("u2", "second" * 200),
        _assistant_with_tool_use("u3", "search_catalog", {"q": "3"}),
        _user_with_tool_result("u3", "third" * 200),
    ]
    out = compact_tool_results(
        messages, retention={"search_catalog": "last_turn_only"}, recent_keep=2
    )
    # First call compacted, last two preserved
    assert "[pruned" in out[1]["content"][0]["content"]
    assert out[3]["content"][0]["content"] == "second" * 200
    assert out[5]["content"][0]["content"] == "third" * 200


# ---------------------------------------------------------------------------
# Session-mode preservation — cart calls stay intact
# ---------------------------------------------------------------------------


def test_session_retention_preserves_old_tool_results():
    messages = [
        _assistant_with_tool_use("c1", "create_cart", {"items": ["x"]}),
        _user_with_tool_result("c1", "cart-state-blob" * 200),
        _assistant_with_tool_use("u1", "search_catalog", {"q": "y"}),
        _user_with_tool_result("u1", "search-blob" * 200),
    ]
    out = compact_tool_results(
        messages,
        retention={
            "create_cart": "session",
            "search_catalog": "last_turn_only",
        },
        recent_keep=1,
    )
    # Cart result preserved despite being older
    assert out[1]["content"][0]["content"] == "cart-state-blob" * 200
    # Search result is the most recent, so also preserved by recent_keep
    assert out[3]["content"][0]["content"] == "search-blob" * 200


def test_unknown_tool_defaults_to_session_no_compaction():
    """A tool not in the retention map is treated as 'session' (kept)."""
    messages = [
        _assistant_with_tool_use("x1", "mystery_tool", {}),
        _user_with_tool_result("x1", "mystery-blob" * 200),
        _assistant_with_tool_use("x2", "mystery_tool", {}),
        _user_with_tool_result("x2", "mystery2" * 200),
    ]
    out = compact_tool_results(
        messages, retention={"search_catalog": "last_turn_only"}, recent_keep=1
    )
    assert out[1]["content"][0]["content"] == "mystery-blob" * 200


# ---------------------------------------------------------------------------
# Stub formatting
# ---------------------------------------------------------------------------


def test_stub_includes_tool_name_and_args():
    messages = [
        _assistant_with_tool_use("u1", "search_catalog", {"query": "red bottle"}),
        _user_with_tool_result("u1", "big"),
        _assistant_with_tool_use("u2", "search_catalog", {"query": "blue"}),
        _user_with_tool_result("u2", "big"),
    ]
    out = compact_tool_results(
        messages, retention={"search_catalog": "last_turn_only"}, recent_keep=1
    )
    stub = out[1]["content"][0]["content"]
    assert "search_catalog" in stub
    assert "red bottle" in stub  # args echo back
    assert "re-call" in stub.lower()


def test_stub_truncates_huge_args():
    big_query = "x" * 5000
    messages = [
        _assistant_with_tool_use("u1", "search_catalog", {"query": big_query}),
        _user_with_tool_result("u1", "result"),
        _assistant_with_tool_use("u2", "search_catalog", {"query": "small"}),
        _user_with_tool_result("u2", "result"),
    ]
    out = compact_tool_results(
        messages, retention={"search_catalog": "last_turn_only"}, recent_keep=1
    )
    stub = out[1]["content"][0]["content"]
    # The stub is bounded — should be well under the original args size.
    assert len(stub) < 500


# ---------------------------------------------------------------------------
# No-op cases — these are the "don't blow up" guarantees
# ---------------------------------------------------------------------------


def test_empty_messages_returns_empty():
    assert compact_tool_results([], retention={"x": "last_turn_only"}) == []


def test_no_retention_map_returns_messages_unchanged():
    messages = [
        _assistant_with_tool_use("u1", "search_catalog", {}),
        _user_with_tool_result("u1", "blob"),
    ]
    out = compact_tool_results(messages, retention=None)
    assert out[1]["content"][0]["content"] == "blob"


def test_string_content_messages_pass_through():
    """User messages with plain-string content (no tool blocks) shouldn't crash."""
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there!"},
    ]
    out = compact_tool_results(messages, retention={"search_catalog": "last_turn_only"})
    assert out == messages


def test_tool_result_without_matching_tool_use_is_left_alone():
    """If a tool_result has no preceding tool_use block we can resolve, skip it.

    Real Anthropic conversations always pair these; but a corrupted/replayed
    history shouldn't crash the compactor.
    """
    messages = [
        # tool_result whose tool_use_id never appeared as a tool_use
        _user_with_tool_result("orphan", "lonely-blob" * 200),
        _assistant_with_tool_use("u1", "search_catalog", {}),
        _user_with_tool_result("u1", "newest"),
    ]
    out = compact_tool_results(
        messages, retention={"search_catalog": "last_turn_only"}, recent_keep=1
    )
    # The orphan is untouched (no tool name → no policy → preserved).
    assert out[0]["content"][0]["content"] == "lonely-blob" * 200
    # The newest result is also untouched (recent_keep=1).
    assert out[2]["content"][0]["content"] == "newest"


def test_does_not_mutate_input_messages():
    messages = [
        _assistant_with_tool_use("u1", "search_catalog", {}),
        _user_with_tool_result("u1", "blob" * 200),
        _assistant_with_tool_use("u2", "search_catalog", {}),
        _user_with_tool_result("u2", "newest"),
    ]
    original_blob = messages[1]["content"][0]["content"]
    compact_tool_results(
        messages, retention={"search_catalog": "last_turn_only"}, recent_keep=1
    )
    # Input list untouched (compactor returns a new list, copy-on-write).
    assert messages[1]["content"][0]["content"] == original_blob


# ---------------------------------------------------------------------------
# Identity projection — keep durable referents instead of a bare stub
# ---------------------------------------------------------------------------

import json  # noqa: E402

# A realistic-ish search_catalog result: heavy fields (description, media,
# full variant blobs) + the identity we must preserve (url/handle/price +
# variant ids). The projection should keep the latter and drop the former.
_SEARCH_RESULT = json.dumps(
    {
        "ucp": {
            "version": "2026-04-08",
            "capabilities": {"x": ["lots", "of", "boilerplate"]},
        },
        "products": [
            {
                "id": "gid://shopify/Product/1",
                "title": "Crossover Bra - Pink",
                "handle": "crossover-bra-pink",
                "url": "https://shop.example.com/products/crossover-bra-pink",
                "description": "A very long description " * 50,
                "price_range": {"min": {"amount": "1,699.00", "currency": "INR"}},
                "media": [{"url": "https://cdn/x.jpg"}] * 10,
                "variants": [
                    {
                        "id": "gid://shopify/ProductVariant/11",
                        "title": "Pink / S",
                        "available": True,
                        "media": [{"url": "https://cdn/v.jpg"}] * 5,
                    },
                    {
                        "id": "gid://shopify/ProductVariant/12",
                        "title": "Pink / M",
                        "available": False,
                        "media": [{"url": "https://cdn/v2.jpg"}] * 5,
                    },
                ],
            }
        ],
        "pagination": {"has_next_page": False},
        "_ui_instructions": {"big": "blob " * 100},
    }
)

_KEEP = [
    "products[*].id",
    "products[*].title",
    "products[*].handle",
    "products[*].url",
    "products[*].price_range.min.amount",
    "products[*].variants[*].id",
    "products[*].variants[*].title",
    "products[*].variants[*].available",
    "pagination",
]


def test_projection_keeps_identity_and_drops_heavy_fields():
    messages = [
        _assistant_with_tool_use("u1", "search_catalog", {"query": "pink bra"}),
        _user_with_tool_result("u1", _SEARCH_RESULT),
        _assistant_with_tool_use("u2", "search_catalog", {"query": "leggings"}),
        _user_with_tool_result("u2", _SEARCH_RESULT),  # newest, kept full
    ]
    out = compact_tool_results(
        messages,
        retention={"search_catalog": "last_turn_only"},
        recent_keep=1,
        projection={"search_catalog": _KEEP},
    )
    # Older result (u1) is PROJECTED, not stubbed.
    projected_raw = out[1]["content"][0]["content"]
    assert "[pruned:" not in projected_raw
    proj = json.loads(projected_raw)
    p = proj["products"][0]
    # identity preserved
    assert p["url"] == "https://shop.example.com/products/crossover-bra-pink"
    assert p["handle"] == "crossover-bra-pink"
    assert p["price_range"]["min"]["amount"] == "1,699.00"
    # variant ids preserved (needed to re-add to cart without re-search)
    assert [v["id"] for v in p["variants"]] == [
        "gid://shopify/ProductVariant/11",
        "gid://shopify/ProductVariant/12",
    ]
    assert p["variants"][0]["available"] is True
    # heavy fields dropped
    assert "description" not in p
    assert "media" not in p
    assert "media" not in p["variants"][0]
    assert "ucp" not in proj
    assert "_ui_instructions" not in proj
    # carries the re-call hint
    assert "_pruned" in proj
    # massively smaller than the original
    assert len(projected_raw) < len(_SEARCH_RESULT) // 5
    # newest (u2) kept full
    assert out[3]["content"][0]["content"] == _SEARCH_RESULT


def test_projection_absent_falls_back_to_stub():
    # last_turn_only tool with NO projection keep-list → still stubbed.
    messages = [
        _assistant_with_tool_use("u1", "search_catalog", {"query": "x"}),
        _user_with_tool_result("u1", _SEARCH_RESULT),
        _assistant_with_tool_use("u2", "search_catalog", {"query": "y"}),
        _user_with_tool_result("u2", "newest"),
    ]
    out = compact_tool_results(
        messages,
        retention={"search_catalog": "last_turn_only"},
        recent_keep=1,
        projection={"get_cart": ["line_items[*].id"]},  # different tool
    )
    assert "[pruned: search_catalog" in out[1]["content"][0]["content"]


def test_projection_non_json_content_falls_back_to_stub():
    messages = [
        _assistant_with_tool_use("u1", "search_catalog", {"query": "x"}),
        _user_with_tool_result("u1", "not-json-just-text" * 50),
        _assistant_with_tool_use("u2", "search_catalog", {"query": "y"}),
        _user_with_tool_result("u2", "newest"),
    ]
    out = compact_tool_results(
        messages,
        retention={"search_catalog": "last_turn_only"},
        recent_keep=1,
        projection={"search_catalog": _KEEP},
    )
    # Unparseable content can't be projected → stub keeps it bounded.
    assert "[pruned: search_catalog" in out[1]["content"][0]["content"]


# ---------------------------------------------------------------------------
# Universal-shape variant (Gemini path)
# ---------------------------------------------------------------------------

from app.ai.voice.agents.breeze_buddy.chat.history.compactor import (  # noqa: E402
    compact_tool_results_universal,
)


def _universal_exchange(call_id: str, tool: str, result: dict) -> list:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool, "arguments": '{"query":"red"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result),
        },
    ]


def test_universal_compacts_stale_last_turn_only_results():
    messages = [
        {"role": "user", "content": "find shoes"},
        *_universal_exchange("c1", "search_catalog", {"products": [{"id": "p1"}] * 40}),
        {"role": "user", "content": "more"},
        *_universal_exchange("c2", "search_catalog", {"products": [{"id": "p2"}]}),
    ]
    out = compact_tool_results_universal(
        messages, retention={"search_catalog": "last_turn_only"}, recent_keep=1
    )
    stale = next(m for m in out if m.get("tool_call_id") == "c1")
    fresh = next(m for m in out if m.get("tool_call_id") == "c2")
    assert "[pruned: search_catalog(" in stale["content"]
    assert json.loads(fresh["content"]) == {"products": [{"id": "p2"}]}
    # Assistant tool_calls entries are untouched (proof the tool ran).
    assert any(
        c["id"] == "c1"
        for m in out
        if m.get("role") == "assistant"
        for c in m.get("tool_calls") or []
    )
    # Input not mutated.
    assert "[pruned" not in messages[2]["content"]


def test_universal_projection_keeps_identity_paths():
    result = {
        "products": [
            {"id": "p1", "url": "https://s/p1", "description": "x" * 500},
            {"id": "p2", "url": "https://s/p2", "description": "y" * 500},
        ]
    }
    messages = [
        *_universal_exchange("c1", "search_catalog", result),
        *_universal_exchange("c2", "search_catalog", {"products": []}),
    ]
    out = compact_tool_results_universal(
        messages,
        retention={"search_catalog": "last_turn_only"},
        recent_keep=1,
        projection={"search_catalog": ["products[*].id", "products[*].url"]},
    )
    stale = json.loads(next(m for m in out if m.get("tool_call_id") == "c1")["content"])
    assert stale["products"] == [
        {"id": "p1", "url": "https://s/p1"},
        {"id": "p2", "url": "https://s/p2"},
    ]
    assert "_pruned" in stale


def test_universal_passes_non_dict_entries_through():
    class _Specific:  # stand-in for LLMSpecificMessage
        pass

    marker = _Specific()
    messages = [
        {"role": "user", "content": "hi"},
        marker,
        *_universal_exchange("c1", "search_catalog", {"products": []}),
    ]
    out = compact_tool_results_universal(
        messages, retention={"search_catalog": "last_turn_only"}
    )
    assert out[1] is marker


def test_universal_session_retention_untouched():
    messages = [
        *_universal_exchange("c1", "get_cart", {"id": "cart1"}),
        *_universal_exchange("c2", "get_cart", {"id": "cart1"}),
    ]
    out = compact_tool_results_universal(messages, retention={}, recent_keep=1)
    assert all("[pruned" not in (m.get("content") or "") for m in out)


# ---------------------------------------------------------------------------
# Voice variant — role:"developer" async_tool envelope (MCP tools)
# ---------------------------------------------------------------------------

from app.ai.voice.agents.breeze_buddy.agent.context_compactor import (  # noqa: E402
    CompactedAssistantAggregator,
    CompactedContextAggregatorPair,
    build_tool_context_maps,
    compact_voice_tool_results,
)


def _async_tool_envelope(call_id: str, result: dict) -> dict:
    """The role:'developer' message pipecat stores for an async MCP tool call."""
    return {
        "role": "developer",
        "content": json.dumps(
            {
                "type": "async_tool",
                "tool_call_id": call_id,
                "status": "finished",
                "description": "the result",
                "result": json.dumps(result),
            }
        ),
    }


def _voice_exchange(call_id: str, tool: str, result: dict) -> list:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool, "arguments": '{"query":"red"}'},
                }
            ],
        },
        _async_tool_envelope(call_id, result),
    ]


def test_voice_compacts_stale_async_tool_results_recent_keep_zero():
    """With recent_keep=0 (the voice default), ALL async_tool results —
    including the latest — are compacted to a stub."""
    messages = [
        {"role": "user", "content": "find shoes"},
        *_voice_exchange("c1", "search_catalog", {"products": [{"id": "p1"}] * 40}),
        {"role": "user", "content": "more"},
        *_voice_exchange("c2", "search_catalog", {"products": [{"id": "p2"}]}),
    ]
    out = compact_voice_tool_results(
        messages, retention={"search_catalog": "last_turn_only"}, recent_keep=0
    )
    # Unlike the chat compactor's recent_keep=1, BOTH results are stubbed.
    dev = [m for m in out if m.get("role") == "developer"]
    stale = next(m for m in dev if "c1" in m["content"])
    fresh = next(m for m in dev if "c2" in m["content"])
    assert "[pruned: search_catalog(" in json.loads(stale["content"])["result"]
    assert "[pruned: search_catalog(" in json.loads(fresh["content"])["result"]


def test_voice_projection_keeps_identity_paths():
    result = {
        "products": [
            {"id": "p1", "url": "https://s/p1", "description": "x" * 500},
            {"id": "p2", "url": "https://s/p2", "description": "y" * 500},
        ]
    }
    messages = [
        *_voice_exchange("c1", "search_catalog", result),
        *_voice_exchange("c2", "search_catalog", {"products": []}),
    ]
    out = compact_voice_tool_results(
        messages,
        retention={"search_catalog": "last_turn_only"},
        recent_keep=0,
        projection={"search_catalog": ["products[*].id", "products[*].url"]},
    )
    stale = json.loads(json.loads(out[1]["content"])["result"])
    assert stale["products"] == [
        {"id": "p1", "url": "https://s/p1"},
        {"id": "p2", "url": "https://s/p2"},
    ]
    assert "_pruned" in stale


def test_voice_session_tool_untouched():
    messages = [
        *_voice_exchange("c1", "get_cart", {"id": "cart1"}),
        *_voice_exchange("c2", "get_cart", {"id": "cart1"}),
    ]
    out = compact_voice_tool_results(
        messages,
        retention={"get_cart": "session", "search_catalog": "last_turn_only"},
        recent_keep=0,
    )
    assert all("[pruned" not in (m.get("content") or "") for m in out)


def test_voice_non_async_developer_message_touched():
    """A role:'developer' message that is NOT an async_tool envelope is left alone."""
    messages = [
        {"role": "developer", "content": json.dumps({"reasoning": "plain note"})},
        *_voice_exchange("c1", "search_catalog", {"products": [{"id": "p1"}]}),
        {"role": "user", "content": "ok"},
    ]
    out = compact_voice_tool_results(
        messages, retention={"search_catalog": "last_turn_only"}, recent_keep=0
    )
    assert out[0]["content"] == json.dumps({"reasoning": "plain note"})
    dev = next(m for m in out if m.get("role") == "developer" and "c1" in m["content"])
    assert "[pruned: search_catalog" in json.loads(dev["content"])["result"]


def test_voice_unknown_tool_defaults_to_no_compaction():
    messages = [
        *_voice_exchange("x1", "mystery_tool", {"big": "blob" * 200}),
        *_voice_exchange("x2", "mystery_tool", {"big": "blob2" * 200}),
    ]
    out = compact_voice_tool_results(
        messages, retention={"search_catalog": "last_turn_only"}, recent_keep=0
    )
    assert all("[pruned" not in (m.get("content") or "") for m in out)


def test_voice_empty_retention_is_noop():
    messages = [*_voice_exchange("c1", "search_catalog", {"products": []})]
    out = compact_voice_tool_results(messages, retention=None, recent_keep=0)
    assert out == messages


def test_voice_does_not_mutate_input():
    messages = [*_voice_exchange("c1", "search_catalog", {"products": [{"id": "p"}]})]
    original = messages[1]["content"]
    compact_voice_tool_results(
        messages, retention={"search_catalog": "last_turn_only"}, recent_keep=0
    )
    assert messages[1]["content"] == original


def test_build_tool_context_maps_from_config():
    """Maps are pulled from the template's MCP server config (voice mirror of
    chat tooling). A server with no fields yields empty maps (no-op)."""

    class _Server:
        tool_context_retention = {"search_catalog": "last_turn_only"}
        tool_context_projection = {"search_catalog": ["products[*].id"]}

    class _ServerEmpty:
        tool_context_retention = None
        tool_context_projection = None

    class _Mcp:
        servers = [_Server(), _ServerEmpty()]

    class _Config:
        mcp = _Mcp()

    retention, projection = build_tool_context_maps(_Config())
    assert retention == {"search_catalog": "last_turn_only"}
    assert projection == {"search_catalog": ["products[*].id"]}

    retention2, projection2 = build_tool_context_maps(None)
    assert retention2 == {}
    assert projection2 == {}


def test_voice_projection_unwraps_status_data_envelope():
    """MCP HTTP tool results are stored as ``{"status":"success","data":"<json>"}``
    (mcp/__init__.py wraps them). The projection must unwrap that envelope so
    ``products[*].title`` resolves against the real payload instead of stubbing.
    This is the regression that made the voice agent reply 'I couldn't confirm'
    despite search_catalog returning rich results."""
    paylod = {
        "products": [
            {
                "id": "p1",
                "title": "Ayurvedic Medicine for Kidney",
                "price": "999.00",
                "description": "x" * 500,
            },
            {
                "id": "p2",
                "title": "GFR Powder",
                "price": "699.00",
                "description": "y" * 500,
            },
        ]
    }
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "search_catalog",
                        "arguments": '{"query":"kidney"}',
                    },
                }
            ],
        },
        {
            "role": "developer",
            "content": json.dumps(
                {
                    "type": "async_tool",
                    "tool_call_id": "c1",
                    "status": "finished",
                    "description": "the result",
                    "result": json.dumps(
                        {"status": "success", "data": json.dumps(paylod)}
                    ),
                }
            ),
        },
    ]
    out = compact_voice_tool_results(
        messages,
        retention={"search_catalog": "last_turn_only"},
        recent_keep=0,
        projection={
            "search_catalog": [
                "products[*].id",
                "products[*].title",
                "products[*].price",
            ]
        },
    )
    dev = [
        m
        for m in out
        if isinstance(m, dict) and m.get("role") == "developer" and "c1" in m["content"]
    ]
    envelope = json.loads(dev[0]["content"])
    # NOT a stub — the titles were projected through the envelope.
    assert "[pruned: search_catalog" not in envelope["result"]
    projected = json.loads(envelope["result"])
    assert projected["products"] == [
        {"id": "p1", "title": "Ayurvedic Medicine for Kidney", "price": "999.00"},
        {"id": "p2", "title": "GFR Powder", "price": "699.00"},
    ]
    assert "_pruned" in projected
    assert "description" not in projected["products"][0]


def test_voice_projection_unwraps_object_data_and_plain_result():
    """_unwrap_result handles both a JSON-string ``data`` and an object
    ``data``, and passes through a non-envelope result unchanged."""
    object_data = {
        "role": "developer",
        "content": json.dumps(
            {
                "type": "async_tool",
                "tool_call_id": "c2",
                "status": "finished",
                "description": "the result",
                "result": json.dumps(
                    {"status": "success", "data": {"products": [{"id": "p9"}]}}
                ),
            }
        ),
    }
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "search_catalog", "arguments": "{}"},
                }
            ],
        },
        object_data,
    ]
    out = compact_voice_tool_results(
        messages,
        retention={"search_catalog": "last_turn_only"},
        recent_keep=0,
        projection={"search_catalog": ["products[*].id"]},
    )
    projected = json.loads(json.loads(out[1]["content"])["result"])
    assert projected["products"] == [{"id": "p9"}]
    assert "_pruned" in projected

    plain = {
        "role": "developer",
        "content": json.dumps(
            {
                "type": "async_tool",
                "tool_call_id": "c3",
                "status": "finished",
                "description": "the result",
                "result": json.dumps({"products": [{"id": "p3"}]}),
            }
        ),
    }
    messages2 = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c3",
                    "type": "function",
                    "function": {"name": "search_catalog", "arguments": "{}"},
                }
            ],
        },
        plain,
    ]
    out2 = compact_voice_tool_results(
        messages2,
        retention={"search_catalog": "last_turn_only"},
        recent_keep=0,
        projection={"search_catalog": ["products[*].id"]},
    )
    projected2 = json.loads(json.loads(out2[1]["content"])["result"])
    assert projected2["products"] == [{"id": "p3"}]
    assert "_pruned" in projected2


def test_pair_returns_compacting_assistant_shared_with_pipeline():
    """CompactedContextAggregatorPair.assistant() yields the same compacting
    instance the pipeline uses, so observer turn events keep firing."""
    from pipecat.processors.aggregators.llm_context import LLMContext

    ctx = LLMContext()
    pair = CompactedContextAggregatorPair(
        ctx,
        retention={"search_catalog": "last_turn_only"},
        projection={"search_catalog": ["products[*].id"]},
        recent_keep=0,
    )
    assistant = pair.assistant()
    assert isinstance(assistant, CompactedAssistantAggregator)
    assert pair.assistant() is assistant
    assert pair.user() is not None
    assert assistant._compact_retention == {"search_catalog": "last_turn_only"}
    assert assistant._compact_projection == {"search_catalog": ["products[*].id"]}
    assert assistant._compact_recent_keep == 0

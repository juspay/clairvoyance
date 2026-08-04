"""Tests for the A3 compact wire form expander (``chat/ui_stream.py``).

Covers ``expand_compact_op`` / ``expand_compact_line``: add/replace/remove
shorthand, the ``{"kv":[k,v]}`` body shorthand, canonical + malformed
pass-through, and an end-to-end check that a compact line through
``process_op_line`` yields the same canonical ``ui_op`` as its canonical
equivalent.
"""

from __future__ import annotations

import json

from app.ai.voice.agents.breeze_buddy.chat.ui.stream import (
    expand_compact_line,
    expand_compact_op,
    expand_repeat_line,
    process_op_line,
)


def test_compact_add_with_parent() -> None:
    out = expand_compact_op({"+": "p1:Tile@root", "title": "Dawn"})
    assert out == {
        "op": "add",
        "id": "p1",
        "type": "Tile",
        "parent": "root",
        "props": {"title": "Dawn"},
    }


def test_compact_add_root_omits_parent() -> None:
    out = expand_compact_op({"+": "root:Stack", "gap": "md"})
    assert out == {"op": "add", "id": "root", "type": "Stack", "props": {"gap": "md"}}
    assert "parent" not in out


def test_compact_replace() -> None:
    out = expand_compact_op({"~": "p1", "title": "New"})
    assert out == {"op": "replace", "id": "p1", "props": {"title": "New"}}


def test_compact_remove() -> None:
    out = expand_compact_op({"-": "p1"})
    assert out == {"op": "remove", "id": "p1"}


def test_kv_body_shorthand_expands() -> None:
    out = expand_compact_op({"+": "p1:Tile@root", "body": [{"kv": ["Price", "₹699"]}]})
    assert out["props"]["body"] == [
        {"kind": "key_value", "key": "Price", "value": "₹699"}
    ]


def test_canonical_body_item_left_untouched() -> None:
    item = {"kind": "key_value", "key": "Price", "value": "₹1"}
    out = expand_compact_op({"+": "p1:Tile@root", "body": [item]})
    assert out["props"]["body"] == [item]


def test_canonical_op_passes_through_identically() -> None:
    canonical = {"op": "add", "id": "p1", "type": "Tile", "parent": "root", "props": {}}
    assert expand_compact_op(canonical) is canonical


def test_malformed_compact_spec_passes_through() -> None:
    # No "id:Type" colon — can't expand; return unchanged so the parser drops it.
    bad = {"+": "justanid", "title": "x"}
    assert expand_compact_op(bad) is bad


def test_expand_line_canonical_passthrough_is_verbatim() -> None:
    line = '{"op":"add","id":"root","type":"Stack"}'
    assert expand_compact_line(line) == line


def test_expand_line_non_json_and_non_dict_passthrough() -> None:
    assert expand_compact_line("not json") == "not json"
    assert expand_compact_line("[1,2,3]") == "[1,2,3]"  # valid JSON, but not a dict


def test_expand_line_produces_canonical_json() -> None:
    out = expand_compact_line('{"+":"p1:Tile@root","title":"Dawn"}')
    assert json.loads(out) == {
        "op": "add",
        "id": "p1",
        "type": "Tile",
        "parent": "root",
        "props": {"title": "Dawn"},
    }


def test_compact_and_canonical_yield_same_ui_op_end_to_end() -> None:
    """A compact line and its canonical equivalent must produce identical
    ui_op events through the full process_op_line pipeline (no healer)."""
    compact = '{"+":"t1:Tile@root","title":"Dawn","body":[{"kv":["Price","₹699"]}]}'
    canonical = (
        '{"op":"add","id":"t1","type":"Tile","parent":"root","props":'
        '{"title":"Dawn","body":[{"kind":"key_value","key":"Price","value":"₹699"}]}}'
    )
    ev_c = [e for e in process_op_line(compact) if e.event == "ui_op"]
    ev_k = [e for e in process_op_line(canonical) if e.event == "ui_op"]
    assert len(ev_c) == 1 and len(ev_k) == 1
    assert ev_c[0].data["op"] == ev_k[0].data["op"]


# ---------------------------------------------------------------------------
# A1 — repeat / $item expansion
# ---------------------------------------------------------------------------


def _parse_lines(lines: list[str]) -> list[dict]:
    return [json.loads(line) for line in lines]


def test_repeat_fans_out_one_op_per_row_with_keyed_ids() -> None:
    line = json.dumps(
        {
            "op": "add",
            "id": "tile",
            "type": "Tile",
            "parent": "root",
            "repeat": {
                "items": [{"id": "a", "title": "Alpha"}, {"id": "b", "title": "Beta"}],
                "key": "id",
            },
            "props": {"title": {"$item": "title"}},
        }
    )
    out = _parse_lines(expand_repeat_line(line))
    assert [o["id"] for o in out] == ["tile-a", "tile-b"]
    assert [o["props"]["title"] for o in out] == ["Alpha", "Beta"]
    # repeat is stripped from the instances; structure carries through
    assert all("repeat" not in o for o in out)
    assert all(o["type"] == "Tile" and o["parent"] == "root" for o in out)


def test_repeat_without_key_uses_index() -> None:
    line = json.dumps(
        {
            "op": "add",
            "id": "t",
            "type": "Tile",
            "parent": "root",
            "repeat": {"items": [{"title": "X"}, {"title": "Y"}]},
            "props": {"title": {"$item": "title"}},
        }
    )
    out = _parse_lines(expand_repeat_line(line))
    assert [o["id"] for o in out] == ["t-0", "t-1"]


def test_item_dotted_path_and_nested_binding() -> None:
    line = json.dumps(
        {
            "op": "add",
            "id": "t",
            "type": "Tile",
            "parent": "root",
            "repeat": {"items": [{"id": "1", "v": {"img": "u1.jpg"}}], "key": "id"},
            "props": {"media": {"src": {"$item": "v.img"}, "alt": "x"}},
        }
    )
    out = _parse_lines(expand_repeat_line(line))
    assert out[0]["props"]["media"] == {"src": "u1.jpg", "alt": "x"}


def test_item_missing_field_resolves_to_none() -> None:
    line = json.dumps(
        {
            "op": "add",
            "id": "t",
            "type": "Tile",
            "parent": "root",
            "repeat": {"items": [{"id": "1"}], "key": "id"},
            "props": {"title": {"$item": "title"}},
        }
    )
    out = _parse_lines(expand_repeat_line(line))
    assert out[0]["props"]["title"] is None


def test_repeat_empty_or_invalid_items_emits_nothing() -> None:
    base = {"op": "add", "id": "t", "type": "Tile", "parent": "root", "props": {}}
    assert expand_repeat_line(json.dumps({**base, "repeat": {"items": []}})) == []
    assert expand_repeat_line(json.dumps({**base, "repeat": {"items": "nope"}})) == []


def test_no_repeat_passes_line_through_verbatim() -> None:
    line = '{"op":"add","id":"root","type":"Stack"}'
    assert expand_repeat_line(line) == [line]


def test_compact_repeat_hoists_and_expands_end_to_end() -> None:
    """A compact repeat template emits one validated ui_op per row through the
    full pipeline, binding each row's data — including a per-item 'red variant'
    image the LLM placed in the data array (emergent choice preserved)."""
    compact = json.dumps(
        {
            "+": "tile:Tile@root",
            "repeat": {
                "items": [
                    {
                        "id": "9",
                        "title": "Red Bottle A",
                        "img": "https://cdn/red-a.jpg",
                    },
                    {
                        "id": "8",
                        "title": "Red Bottle B",
                        "img": "https://cdn/red-b.jpg",
                    },
                ],
                "key": "id",
            },
            "title": {"$item": "title"},
            "media": {"src": {"$item": "img"}, "alt": {"$item": "title"}},
        }
    )
    ui_ops = [e.data["op"] for e in process_op_line(compact) if e.event == "ui_op"]
    assert len(ui_ops) == 2
    assert [o["id"] for o in ui_ops] == ["tile-9", "tile-8"]
    assert ui_ops[0]["type"] == "Tile" and ui_ops[0]["parent"] == "root"
    assert ui_ops[0]["props"]["title"] == "Red Bottle A"
    assert ui_ops[0]["props"]["media"]["src"] == "https://cdn/red-a.jpg"
    assert ui_ops[1]["props"]["media"]["src"] == "https://cdn/red-b.jpg"


def test_malformed_repeat_emits_observable_drop() -> None:
    """A repeat whose items isn't a list is surfaced as ui_op_dropped
    (reason=repeat_items_not_list) instead of silently rendering nothing."""
    line = json.dumps(
        {
            "op": "add",
            "id": "t",
            "type": "Tile",
            "parent": "root",
            "repeat": {"items": "nope"},
            "props": {},
        }
    )
    events = process_op_line(line)
    dropped = [e for e in events if e.event == "ui_op_dropped"]
    assert len(dropped) == 1
    assert dropped[0].data["reason"] == "repeat_items_not_list"
    # Structural-only op signature — no resolved payload (props/repeat) leaks.
    assert dropped[0].data["op"].get("type") == "Tile"
    assert "props" not in dropped[0].data["op"]
    assert "repeat" not in dropped[0].data["op"]


def test_empty_repeat_items_is_not_flagged() -> None:
    """An empty ``items: []`` is a valid no-op — no rows and no drop event."""
    line = json.dumps(
        {
            "op": "add",
            "id": "t",
            "type": "Tile",
            "parent": "root",
            "repeat": {"items": []},
            "props": {},
        }
    )
    assert process_op_line(line) == []


# ---------------------------------------------------------------------------
# Root anchoring — multiple top-level blocks in one turn (compound requests)
# ---------------------------------------------------------------------------


def test_root_anchor_injected_for_orphan_block() -> None:
    # Block parents to `root` but the model never `add`ed root → inject a
    # neutral Stack root first, so the block has an anchor (else it orphans
    # and the widget renders nothing).
    known: set = set()
    line = (
        '{"op":"add","id":"cart","type":"Stack","parent":"root","props":{"gap":"md"}}'
    )
    ops = [
        e.data["op"]
        for e in process_op_line(line, known_ids=known)
        if e.event == "ui_op"
    ]
    assert [(o["op"], o["id"], o.get("type")) for o in ops] == [
        ("add", "root", "Stack"),
        ("add", "cart", "Stack"),
    ]
    assert "root" in known


def test_replace_root_rescued_to_add_anchor() -> None:
    # A `replace root` before root exists (model assumed a prior-turn root) is
    # rescued into the anchoring add.
    known: set = set()
    line = '{"op":"replace","id":"root","props":{"gap":"md"}}'
    ops = [
        e.data["op"]
        for e in process_op_line(line, known_ids=known)
        if e.event == "ui_op"
    ]
    assert len(ops) == 1
    assert (ops[0]["op"], ops[0]["id"], ops[0]["type"]) == ("add", "root", "Stack")


def test_no_anchor_when_model_adds_root() -> None:
    # Normal single-block render: the block IS root → no injection, untouched.
    known: set = set()
    line = '{"op":"add","id":"root","type":"Carousel","props":{"snap":true}}'
    ops = [
        e.data["op"]
        for e in process_op_line(line, known_ids=known)
        if e.event == "ui_op"
    ]
    assert len(ops) == 1 and ops[0]["id"] == "root" and ops[0]["type"] == "Carousel"


def test_second_block_does_not_duplicate_root() -> None:
    # Two top-level blocks → exactly one injected root; both attach as siblings.
    known: set = set()
    l1 = '{"op":"add","id":"cart","type":"Stack","parent":"root","props":{"gap":"md"}}'
    l2 = '{"op":"add","id":"bottles","type":"Carousel","parent":"root","props":{"snap":true}}'
    process_op_line(l1, known_ids=known)  # anchors root + adds cart
    ops2 = [
        e.data["op"] for e in process_op_line(l2, known_ids=known) if e.event == "ui_op"
    ]
    assert [(o["op"], o["id"]) for o in ops2] == [("add", "bottles")]  # no second root

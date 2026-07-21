# pyrefly: ignore-errors
# Same Pydantic dynamic-attr narrowing limitation as
# test_tile_validation.py — validate_props returns _CatalogBase, concrete
# subclass attrs read as missing to pyrefly.
"""Tests for the SpecStream JSONL emission pipeline.

Covers:
  * UiStreamExtractor — token streaming around ``<ui_stream>`` markers
  * parse_op_line — catalog validation, prop schema enforcement
  * strip_ui_stream_markers — persistence-time prose extraction
  * process_op_line — full healer + parse + validate flow
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.chat.ui_stream import (
    JsonlOpLine,
    TextOut,
    UiStreamExtractor,
    parse_op_line,
    process_op_line,
    strip_ui_stream_markers,
    summarize_ui_ops,
)

# Load template package first so its __init__ chain completes before any
# `chat/*` module imports drag in `handlers.transport.utils.field_resolver`
# mid-load and trip the circular-import guard (same trap as test_session_state).
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    UI_CATALOG,
    is_known_type,
    validate_props,
)

# ---------------------------------------------------------------------------
# UiStreamExtractor
# ---------------------------------------------------------------------------


def _drain(extractor: UiStreamExtractor):
    """Collect everything the extractor yields after a feed + flush cycle."""
    out = list(extractor.flush())
    return out


def test_extractor_yields_prose_outside_markers():
    ex = UiStreamExtractor()
    items = list(ex.feed("hello world"))
    items.extend(_drain(ex))
    assert items == [TextOut(value="hello world")]


def test_extractor_isolates_jsonl_lines_inside_marker():
    ex = UiStreamExtractor()
    items: list = []
    items.extend(ex.feed("here you go <ui_stream>\n"))
    items.extend(ex.feed('{"op":"add","id":"root","type":"Stack"}\n'))
    items.extend(
        ex.feed(
            '{"op":"add","id":"a","type":"Text","parent":"root","props":{"text":"hi"}}\n'
        )
    )
    items.extend(ex.feed("</ui_stream> done"))
    items.extend(_drain(ex))

    types = [type(i).__name__ for i in items]
    assert types == ["TextOut", "JsonlOpLine", "JsonlOpLine", "TextOut"]
    prose = [i.value for i in items if isinstance(i, TextOut)]
    assert prose == ["here you go ", " done"]
    op_lines = [i.raw for i in items if isinstance(i, JsonlOpLine)]
    assert op_lines[0] == '{"op":"add","id":"root","type":"Stack"}'
    assert "Text" in op_lines[1]


def test_extractor_handles_split_marker_across_deltas():
    ex = UiStreamExtractor()
    items: list = []
    # Marker split mid-tag.
    for chunk in [
        "pre <",
        "ui_s",
        "tream>\n",
        '{"op":"remove","id":"x"}',
        "\n</ui_st",
        "ream>",
    ]:
        items.extend(ex.feed(chunk))
    items.extend(_drain(ex))

    text = "".join(i.value for i in items if isinstance(i, TextOut))
    assert text.startswith("pre ")
    ops = [i.raw for i in items if isinstance(i, JsonlOpLine)]
    assert ops == ['{"op":"remove","id":"x"}']


def test_extractor_drops_unclosed_block():
    ex = UiStreamExtractor()
    list(ex.feed('hi <ui_stream>\n{"op":"add","id":"root","type":"Stack"}\n'))
    # Stream ends before </ui_stream>; flush should drop the partial body.
    flushed = list(ex.flush())
    # No JSONL emission from flush; partial line silently dropped (warning logged).
    assert all(not isinstance(i, JsonlOpLine) for i in flushed)


# ---------------------------------------------------------------------------
# parse_op_line
# ---------------------------------------------------------------------------


def test_parse_add_op_validates_against_catalog():
    line = '{"op":"add","id":"root","type":"Stack","props":{"gap":"md"}}'
    r = parse_op_line(line)
    assert r.error is None
    assert r.op == {"op": "add", "id": "root", "type": "Stack", "props": {"gap": "md"}}


def test_parse_add_op_with_typed_primitive():
    line = '{"op":"add","id":"h","type":"Handoff","parent":"root","props":{"reason":"checkout","label":"Pay","url":"https://example.com/c","lifecycle":"popup"}}'
    r = parse_op_line(line)
    assert r.error is None
    assert r.op["props"]["reason"] == "checkout"
    assert r.op["props"]["lifecycle"] == "popup"


def test_parse_rejects_unknown_type():
    line = '{"op":"add","id":"x","type":"NotAPrimitive","parent":"root"}'
    r = parse_op_line(line)
    assert r.op is None
    assert r.error and r.error.startswith("unknown_type")


def test_parse_rejects_unknown_props_on_known_type():
    line = '{"op":"add","id":"x","type":"Tag","parent":"root","props":{"text":"new","weight":"bold"}}'
    r = parse_op_line(line)
    assert r.op is None
    assert r.error and r.error.startswith("props_validation_failed")


def test_parse_rejects_non_root_without_parent():
    line = '{"op":"add","id":"x","type":"Text","props":{"text":"hi"}}'
    r = parse_op_line(line)
    assert r.op is None
    assert r.error == "missing_parent"


def test_parse_root_op_does_not_require_parent():
    line = '{"op":"add","id":"root","type":"Carousel"}'
    r = parse_op_line(line)
    assert r.error is None


def test_parse_remove_op_is_minimal():
    line = '{"op":"remove","id":"c1"}'
    r = parse_op_line(line)
    assert r.error is None
    assert r.op == {"op": "remove", "id": "c1"}


def test_parse_replace_op_requires_props():
    line = '{"op":"replace","id":"c1"}'
    r = parse_op_line(line)
    assert r.error == "replace_missing_props"


def test_parse_rejects_unknown_op_kind():
    line = '{"op":"weird","id":"c1"}'
    r = parse_op_line(line)
    assert r.error and r.error.startswith("unknown_op")


def test_parse_rejects_malformed_json():
    line = '{"op":"add"'
    r = parse_op_line(line)
    assert r.error and r.error.startswith("malformed_json")


# ---------------------------------------------------------------------------
# Catalog manifest
# ---------------------------------------------------------------------------


def test_catalog_contains_all_v1_primitives():
    expected = {
        "Stack",
        "Row",
        "Card",
        "CardHeader",
        "Image",
        "Text",
        "Carousel",
        "Tag",
        "Button",
        "Buttons",
        "Table",
        "Message",
        "Handoff",
        "Tile",
    }
    assert expected.issubset(set(UI_CATALOG.keys()))


def test_catalog_does_not_contain_money_primitive():
    """Money was a commerce-tinted typed primitive — runtime is now
    fully commerce-agnostic; price display lives in template-emitted
    key_value / text body rows."""
    assert "Money" not in UI_CATALOG


def test_is_known_type_negative():
    assert is_known_type("Stack")
    assert not is_known_type("ProductCarousel")
    assert not is_known_type("Money")


def test_validate_props_handoff():
    h = validate_props(
        "Handoff",
        {
            "reason": "checkout",
            "label": "Pay",
            "url": "https://example.com/c",
            "lifecycle": "popup",
        },
    )
    assert h.reason == "checkout"
    assert h.lifecycle.value == "popup"


# ---------------------------------------------------------------------------
# strip_ui_stream_markers
# ---------------------------------------------------------------------------


def test_strip_ui_stream_markers_removes_block_only():
    text = (
        "Here you go:\n"
        '<ui_stream>\n{"op":"add","id":"root","type":"Stack"}\n</ui_stream>\n'
        "let me know if you need more."
    )
    out = strip_ui_stream_markers(text)
    assert "ui_stream" not in out
    assert "Here you go:" in out
    assert "let me know" in out


def test_strip_ui_stream_markers_handles_multiple_blocks():
    text = "a <ui_stream>{}</ui_stream> b <ui_stream>{}</ui_stream> c"
    out = strip_ui_stream_markers(text)
    assert out == "a  b  c"


def test_strip_ui_stream_markers_passthrough_empty():
    assert strip_ui_stream_markers("") == ""
    assert strip_ui_stream_markers("no markers here") == "no markers here"


# ---------------------------------------------------------------------------
# process_op_line — full healer→parse→validate pipeline
# ---------------------------------------------------------------------------


def test_process_emits_ui_op_event_on_clean_line():
    line = '{"op":"add","id":"root","type":"Carousel"}'
    events = process_op_line(line, session_state={}, healer=None, known_ids=set())
    assert len(events) == 1
    assert events[0].event == "ui_op"
    assert events[0].data["op"]["type"] == "Carousel"


def test_process_emits_dropped_event_on_unknown_type():
    line = '{"op":"add","id":"x","type":"WidgetThatDoesntExist","parent":"root"}'
    events = process_op_line(line, session_state={}, healer=None, known_ids={"root"})
    assert any(e.event == "ui_op_dropped" for e in events)


def test_process_known_ids_tracks_add_remove():
    known: set = set()
    process_op_line('{"op":"add","id":"root","type":"Stack"}', known_ids=known)
    assert "root" in known
    process_op_line(
        '{"op":"add","id":"c1","type":"Card","parent":"root"}', known_ids=known
    )
    assert "c1" in known
    process_op_line('{"op":"remove","id":"c1"}', known_ids=known)
    assert "c1" not in known


# ---------------------------------------------------------------------------
# summarize_ui_ops — compact memory of rendered UI for the LLM's next turn
# ---------------------------------------------------------------------------


def _tile(tid: str, title: str) -> dict:
    return {
        "op": "add",
        "id": tid,
        "type": "Tile",
        "parent": "root",
        "props": {"title": title},
    }


def _handoff(hid: str, url: str, reason: str = "checkout") -> dict:
    return {
        "op": "add",
        "id": hid,
        "type": "Handoff",
        "parent": "root",
        "props": {"reason": reason, "label": "Go", "url": url, "lifecycle": "popup"},
    }


def test_summarize_empty_returns_empty_string():
    assert summarize_ui_ops([]) == ""


def test_summarize_handles_only_layout_ops():
    # Stack/Row by themselves carry no shopper-facing content — skip them
    # so we don't pollute history with vacuous "[ui rendered: 1 Stack]" lines.
    ops = [
        {"op": "add", "id": "root", "type": "Stack", "props": {"gap": "md"}},
        {"op": "add", "id": "r1", "type": "Row", "parent": "root", "props": {}},
    ]
    assert summarize_ui_ops(ops) == ""


def test_summarize_tiles_include_titles():
    ops = [
        _tile("t1", "Red Bottle 390ml"),
        _tile("t2", "Green Bottle 520ml"),
    ]
    out = summarize_ui_ops(ops)
    assert out.startswith("[ui rendered: 2 Tile(s):")
    assert "Red Bottle 390ml" in out
    assert "Green Bottle 520ml" in out
    assert out.endswith("]")


def test_summarize_caps_tile_titles_at_eight():
    ops = [_tile(f"t{i}", f"Bottle #{i}") for i in range(12)]
    out = summarize_ui_ops(ops)
    assert "12 Tile(s):" in out
    # Only the first 8 titles appear verbatim
    assert "Bottle #0" in out
    assert "Bottle #7" in out
    assert "Bottle #8" not in out
    assert "(+4 more)" in out


def test_summarize_truncates_long_titles():
    long_title = "X" * 200
    ops = [_tile("t1", long_title)]
    out = summarize_ui_ops(ops)
    # Title is bounded — full 200-char string should not appear verbatim
    assert "X" * 200 not in out
    assert "1 Tile(s):" in out


def test_summarize_mixed_primitives_keeps_ordering():
    ops = [
        _tile("t1", "Red Bottle"),
        _handoff("h1", "https://example.com/checkout/abc"),
        {
            "op": "add",
            "id": "tbl1",
            "type": "Table",
            "parent": "root",
            "props": {"columns": ["A"], "rows": [["1"]]},
        },
    ]
    out = summarize_ui_ops(ops)
    # Tiles first, then Handoffs, then other primitives alphabetically
    assert out.index("Tile") < out.index("Handoff")
    assert out.index("Handoff") < out.index("Table")
    assert "1 Table" in out


def test_summarize_handoff_dedupes_same_url():
    ops = [
        _handoff("h1", "https://example.com/cart/abc", "checkout"),
        _handoff("h2", "https://example.com/cart/abc", "checkout"),
    ]
    out = summarize_ui_ops(ops)
    # Two Handoff ops, one unique URL
    assert "2 Handoff(s)" in out
    assert out.count("https://example.com/cart/abc") == 1


def test_summarize_ignores_replace_and_remove():
    # Only ``add`` ops contribute to the rendered-content summary;
    # replace/remove only update existing nodes.
    ops = [
        _tile("t1", "Red Bottle"),
        {"op": "replace", "id": "t1", "props": {"title": "Updated"}},
        {"op": "remove", "id": "t1"},
    ]
    out = summarize_ui_ops(ops)
    # Only the original add counts
    assert "1 Tile(s)" in out
    assert "Red Bottle" in out


def test_summarize_handles_unknown_type_gracefully():
    ops = [
        {
            "op": "add",
            "id": "x1",
            "type": "FutureThingy",
            "parent": "root",
            "props": {},
        }
    ]
    out = summarize_ui_ops(ops)
    assert "1 FutureThingy" in out


# ---------------------------------------------------------------------------
# Recovery of BARE op-lines emitted WITHOUT the <ui_stream> wrapper
# ---------------------------------------------------------------------------
#
# The LLM stochastically forgets to wrap its SpecStream ops. Without recovery
# those lines leak to the user as raw JSON. The extractor now routes a bare
# op-line into the same JsonlOpLine path a wrapped line takes, so it renders as
# UI; genuine prose is untouched and still streams smoothly.


def _feed_all(deltas):
    """Feed each delta then flush; return the flat list of yielded items."""
    ex = UiStreamExtractor()
    out: list = []
    for d in deltas:
        out.extend(ex.feed(d))
    out.extend(ex.flush())
    return out


def test_bare_op_lines_recovered_as_op_not_prose():
    items = _feed_all(
        [
            '{"+":"root:Card@root"}\n',
            '{"+":"t:Text@root","text":"hi"}\n',
        ]
    )
    assert all(isinstance(i, JsonlOpLine) for i in items)
    assert items[0].raw == '{"+":"root:Card@root"}'
    assert items[1].raw == '{"+":"t:Text@root","text":"hi"}'


def test_verbose_bare_op_line_recovered():
    items = _feed_all(['{"op":"add","id":"r","type":"Stack"}\n'])
    assert len(items) == 1 and isinstance(items[0], JsonlOpLine)


def test_prose_is_never_recovered_as_op():
    items = _feed_all(["Here are the cheapest days to fly.\n"])
    assert items == [TextOut(value="Here are the cheapest days to fly.\n")]


def test_prose_with_braces_stays_prose():
    # Looks jsonish but has no op marker → must remain visible prose.
    items = _feed_all(['{"note":"not an op"}\n'])
    assert len(items) == 1 and isinstance(items[0], TextOut)


def test_bare_op_line_split_across_deltas_is_recovered():
    items = _feed_all(['{"+":"root:', 'Card@root","variant":"hi', 'ghlighted"}\n'])
    assert len(items) == 1 and isinstance(items[0], JsonlOpLine)
    assert items[0].raw == '{"+":"root:Card@root","variant":"highlighted"}'


def test_mixed_prose_then_bare_ops():
    items = _feed_all(
        [
            "Best deal below:\n",
            '{"+":"c:Card@root"}\n',
            '{"+":"t:Text@c","text":"IndiGo"}\n',
        ]
    )
    kinds = [type(i).__name__ for i in items]
    assert kinds == ["TextOut", "JsonlOpLine", "JsonlOpLine"]
    assert items[0].value == "Best deal below:\n"


def test_final_bare_op_line_without_newline_recovered_on_flush():
    # No trailing newline — must still be recovered (not leaked) at flush.
    items = _feed_all(['{"+":"root:Card@root"}'])
    assert len(items) == 1 and isinstance(items[0], JsonlOpLine)


def test_wrapped_ops_still_work_after_recovery_change():
    # Regression: the explicit <ui_stream> path is unchanged.
    items = _feed_all(
        [
            "here <ui_stream>\n",
            '{"op":"add","id":"root","type":"Stack"}\n',
            "</ui_stream> done",
        ]
    )
    kinds = [type(i).__name__ for i in items]
    assert kinds == ["TextOut", "JsonlOpLine", "TextOut"]
    assert items[0].value == "here "
    assert items[2].value == " done"


def test_prose_streams_immediately_without_newline():
    # A prose delta with no newline must emit right away (no buffering stall).
    ex = UiStreamExtractor()
    out = list(ex.feed("streaming prose no newline yet"))
    assert out == [TextOut(value="streaming prose no newline yet")]


# ---------------------------------------------------------------------------
# Contact-CTA URL schemes (mailto/tel) + structured drop reasons
# ---------------------------------------------------------------------------


def test_parse_button_mailto_action_validates():
    line = (
        '{"op":"add","id":"b1","type":"Button","parent":"root",'
        '"props":{"label":"Email us","action":{"type":"open_url",'
        '"url":"mailto:support@example.com"}}}'
    )
    r = parse_op_line(line)
    assert r.error is None
    assert r.op["props"]["action"]["url"].startswith("mailto:")


def test_parse_button_tel_action_validates():
    line = (
        '{"op":"add","id":"b1","type":"Button","parent":"root",'
        '"props":{"label":"Call us","action":{"type":"open_url",'
        '"url":"tel:+911234567890"}}}'
    )
    r = parse_op_line(line)
    assert r.error is None
    assert r.op["props"]["action"]["url"].startswith("tel:")


def test_parse_button_javascript_scheme_still_drops():
    line = (
        '{"op":"add","id":"b1","type":"Button","parent":"root",'
        '"props":{"label":"x","action":{"type":"open_url",'
        '"url":"javascript:alert(1)"}}}'
    )
    r = parse_op_line(line)
    assert r.op is None
    assert r.error and r.error.startswith("props_validation_failed")


def test_parse_validation_error_reason_names_failing_field():
    """Drop reasons must carry the type + field path so [CHAT_METRICS]
    drop_reasons is diagnosable without payload access."""
    line = (
        '{"op":"add","id":"t1","type":"Tag","parent":"root",'
        '"props":{"text":"new","weight":"bold"}}'
    )
    r = parse_op_line(line)
    assert r.op is None
    assert r.error.startswith("props_validation_failed:Tag:")
    assert "weight" in r.error
    # Structural only — the offending input value never appears.
    assert "bold" not in r.error


# ---------------------------------------------------------------------------
# Drop funnel — ui_op_dropped carries evidence; healer drops join the funnel
# ---------------------------------------------------------------------------


def test_validator_drop_event_carries_raw_line():
    line = (
        '{"op":"add","id":"b1","type":"Button","parent":"root",'
        '"props":{"label":"x","action":{"type":"open_url","url":"ftp://no"}}}'
    )
    dropped = [e for e in process_op_line(line) if e.event == "ui_op_dropped"]
    assert len(dropped) == 1
    assert dropped[0].data["raw"] == line
    assert dropped[0].data["op"] == {"op": "add", "id": "b1", "type": "Button"}
    assert dropped[0].data["reason"].startswith("props_validation_failed:Button:")


def test_healer_drop_emits_ui_op_dropped_not_healer_applied():
    """Healer drops must land in the SAME funnel counter as validator drops —
    they were previously invisible in ui_dropped."""
    from app.ai.voice.agents.breeze_buddy.chat.ui_healer import (
        HealerContext,
        make_healer_fn,
    )

    ctx = HealerContext(session_data={}, known_ids={"root"})
    line = '{"op":"add","id":"x","type":"NotARealPrimitive","parent":"root"}'
    events = process_op_line(line, healer=make_healer_fn(ctx), known_ids={"root"})
    names = [e.event for e in events]
    assert "ui_op_dropped" in names
    assert "healer_applied" not in names
    drop = next(e for e in events if e.event == "ui_op_dropped")
    assert drop.data["reason"].startswith("dropped_unknown_type")
    assert drop.data["raw"] == line


def test_healer_repair_note_still_emits_healer_applied():
    from app.ai.voice.agents.breeze_buddy.chat.ui_healer import (
        HealerContext,
        make_healer_fn,
    )

    ctx = HealerContext(session_data={}, known_ids={"root"})
    line = '{"op":"add","id":"t","type":"Tag","parent":"root","props":{"label":"hi"}}'
    events = process_op_line(line, healer=make_healer_fn(ctx), known_ids={"root"})
    names = [e.event for e in events]
    assert "healer_applied" in names
    assert "ui_op" in names
    assert "ui_op_dropped" not in names


def test_turn_metrics_collects_drop_details_but_logs_stay_structural():
    from app.ai.voice.agents.breeze_buddy.chat.metrics import TurnMetrics
    from app.ai.voice.agents.breeze_buddy.chat.ui_stream import ui_op_dropped_event

    line = (
        '{"op":"add","id":"email_btn","type":"Button","parent":"root",'
        '"props":{"label":"Email us","action":{"type":"open_url",'
        '"url":"mailto:x@y.com"}}}'
    )
    tm = TurnMetrics(session_id="s", template_id="t", t0=0.0)
    tm.observe(ui_op_dropped_event(line, "props_validation_failed:Button:action"))
    assert tm.ui_dropped == 1
    assert len(tm.drops) == 1
    assert tm.drops[0]["sig"] == {"op": "add", "id": "email_btn", "type": "Button"}
    assert tm.drops[0]["raw"] == line
    # The log line must never carry the raw payload.
    assert "mailto" not in ";".join(tm.drop_reasons)

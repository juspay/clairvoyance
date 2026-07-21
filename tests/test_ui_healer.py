"""Tests for the in-stream healer — one rule per test."""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.chat.ui_healer import (
    HealerContext,
    heal_op_line,
)

# Load template package before chat/* modules — same circular-import trap.
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (  # noqa: F401
    UI_CATALOG,
)


def _ctx(**overrides) -> HealerContext:
    return HealerContext(
        session_data=overrides.get("session_data", {}),
        known_ids=overrides.get("known_ids", set()),
    )


# ---------------------------------------------------------------------------
# Drop rules
# ---------------------------------------------------------------------------


def test_healer_drops_unknown_type():
    r = heal_op_line(
        '{"op":"add","id":"x","type":"NotARealPrimitive","parent":"root"}',
        _ctx(known_ids={"root"}),
    )
    assert r.drop is True
    assert any(n.startswith("dropped_unknown_type") for n in r.notes)


def test_healer_drops_orphan_non_root_add():
    r = heal_op_line(
        '{"op":"add","id":"x","type":"Text","props":{"text":"hi"}}',
        _ctx(known_ids=set()),
    )
    assert r.drop is True
    assert "dropped_orphan_add" in r.notes


def test_healer_keeps_root_op_without_parent():
    r = heal_op_line(
        '{"op":"add","id":"root","type":"Carousel"}',
        _ctx(known_ids=set()),
    )
    assert r.drop is False
    assert r.line is not None


def test_healer_drops_malformed_json():
    r = heal_op_line('{"op":"add"', _ctx())
    assert r.drop is True
    assert any(n.startswith("dropped_malformed_json") for n in r.notes)


# ---------------------------------------------------------------------------
# Transform rules
# ---------------------------------------------------------------------------


def test_healer_strips_unknown_props():
    r = heal_op_line(
        '{"op":"add","id":"t1","type":"Tag","parent":"root","props":{"text":"Premium","weight":"bold"}}',
        _ctx(known_ids={"root"}),
    )
    assert r.drop is False
    assert any(n.startswith("stripped_unknown_props:weight") for n in r.notes)
    import json

    out = json.loads(r.line)  # type: ignore[arg-type]
    assert "weight" not in out["props"]
    assert out["props"]["text"] == "Premium"


def test_healer_button_default_label():
    r = heal_op_line(
        '{"op":"add","id":"b1","type":"Button","parent":"root","props":{"action":{"type":"to_assistant","msg":"hi"}}}',
        _ctx(known_ids={"root"}),
    )
    assert r.drop is False
    import json

    out = json.loads(r.line)  # type: ignore[arg-type]
    assert out["props"]["label"] == "Continue"
    assert any(n == "button_default_label" for n in r.notes)


def test_healer_tag_flattens_array_text():
    r = heal_op_line(
        '{"op":"add","id":"t1","type":"Tag","parent":"root","props":{"text":"[\\"new\\", \\"sale\\"]"}}',
        _ctx(known_ids={"root"}),
    )
    assert r.drop is False
    import json

    out = json.loads(r.line)  # type: ignore[arg-type]
    assert out["props"]["text"] == "new, sale"
    assert any(n == "tag_flattened_array_text" for n in r.notes)


def test_healer_dedupes_id():
    known = {"c1"}
    r = heal_op_line(
        '{"op":"add","id":"c1","type":"Card","parent":"root"}',
        _ctx(known_ids=known | {"root"}),
    )
    assert r.drop is False
    import json

    out = json.loads(r.line)  # type: ignore[arg-type]
    assert out["id"] == "c1__2"
    assert any(n.startswith("renamed_duplicate_id:c1->c1__2") for n in r.notes)


def test_healer_passthrough_clean_line():
    r = heal_op_line(
        '{"op":"add","id":"root","type":"Stack","props":{"gap":"md"}}',
        _ctx(),
    )
    assert r.drop is False
    assert r.notes == []
    assert r.line is not None


# ---------------------------------------------------------------------------
# Alias rename — derived from real-traffic ui_op_dropped telemetry
# ---------------------------------------------------------------------------


def test_healer_renames_tag_label_to_text():
    """The single most common real-traffic miss: Tag.props.label → Tag.text."""
    r = heal_op_line(
        '{"op":"add","id":"t1","type":"Tag","parent":"root","props":{"label":"Premium"}}',
        _ctx(known_ids={"root"}),
    )
    assert r.drop is False
    import json

    out = json.loads(r.line)  # type: ignore[arg-type]
    assert out["props"]["text"] == "Premium"
    assert "label" not in out["props"]
    assert any("renamed_alias_props:Tag:label->text" in n for n in r.notes)


def test_healer_renames_text_content_to_text():
    """Legacy DSL-era prop: Text.props.content → Text.props.text."""
    r = heal_op_line(
        '{"op":"add","id":"x","type":"Text","parent":"root","props":{"content":"Hello","variant":"heading"}}',
        _ctx(known_ids={"root"}),
    )
    assert r.drop is False
    import json

    out = json.loads(r.line)  # type: ignore[arg-type]
    assert out["props"]["text"] == "Hello"


def test_healer_renames_image_url_to_src_and_title_to_alt():
    r = heal_op_line(
        '{"op":"add","id":"i","type":"Image","parent":"root","props":{"url":"https://x/y.jpg","title":"a snowboard"}}',
        _ctx(known_ids={"root"}),
    )
    assert r.drop is False
    import json

    out = json.loads(r.line)  # type: ignore[arg-type]
    assert out["props"]["src"] == "https://x/y.jpg"
    assert out["props"]["alt"] == "a snowboard"


def test_healer_alias_does_not_override_canonical_when_both_present():
    """LLM intent wins: if `text` is already set, don't overwrite from `label`."""
    r = heal_op_line(
        '{"op":"add","id":"t1","type":"Tag","parent":"root","props":{"text":"explicit","label":"fallback"}}',
        _ctx(known_ids={"root"}),
    )
    assert r.drop is False
    import json

    out = json.loads(r.line)  # type: ignore[arg-type]
    assert out["props"]["text"] == "explicit"
    # `label` was stripped (unknown prop), not renamed.
    assert "label" not in out["props"]


def test_healer_infers_missing_op_as_add():
    """LLM omitted the `op` discriminator on an add-shaped line → healed."""
    import json

    r = heal_op_line(
        '{"id":"x","type":"Text","parent":"root","props":{"text":"hi"}}',
        _ctx(known_ids={"root"}),
    )
    assert r.drop is False
    assert "inferred_missing_op:add" in r.notes
    assert json.loads(r.line)["op"] == "add"  # type: ignore[arg-type]


def test_healer_does_not_infer_op_without_type_and_id():
    """Anything less add-shaped passes through untouched (drops downstream)."""
    r = heal_op_line('{"id":"x","props":{}}', _ctx())
    assert r.drop is False
    assert r.notes == []

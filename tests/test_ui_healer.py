"""Tests for the in-stream healer — one rule per test."""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.chat.ui_healer import (
    HealerContext,
    heal_op_line,
)

# Load template package before chat/* modules — same circular-import trap.
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (  # noqa: F401
    QUICK_REPLIES_MAX_ITEMS,
    UI_CATALOG,
    validate_props,
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


# ---------------------------------------------------------------------------
# QuickReplies item-cap clamp (chiclets resilience)
# ---------------------------------------------------------------------------


def _qr_items(n: int):
    return [{"label": f"Category {i}"} for i in range(n)]


def test_quickreplies_cap_is_twelve():
    """Lock the chiclet cap: 12 items validate, 13 do not."""
    assert QUICK_REPLIES_MAX_ITEMS == 12
    validate_props("QuickReplies", {"items": _qr_items(12)})  # no raise
    raised = False
    try:
        validate_props("QuickReplies", {"items": _qr_items(13)})
    except Exception:
        raised = True
    assert raised, "13 items should exceed the QuickReplies cap"


def test_healer_clamps_quickreplies_over_cap():
    """>cap QuickReplies → truncate to the cap (and the clamped op then passes
    catalog validation) instead of dropping the whole row."""
    import json

    line = json.dumps(
        {
            "op": "add",
            "id": "qr",
            "type": "QuickReplies",
            "parent": "root",
            "props": {"items": _qr_items(14)},
        }
    )
    r = heal_op_line(line, _ctx(known_ids={"root"}))
    assert r.drop is False
    assert any(n == "clamped_quickreplies_items:-2" for n in r.notes)
    out = json.loads(r.line)  # type: ignore[arg-type]
    assert len(out["props"]["items"]) == QUICK_REPLIES_MAX_ITEMS
    # the healed op now survives catalog validation
    validate_props("QuickReplies", out["props"])


def test_healer_leaves_quickreplies_at_cap():
    import json

    line = json.dumps(
        {
            "op": "add",
            "id": "qr",
            "type": "QuickReplies",
            "parent": "root",
            "props": {"items": _qr_items(QUICK_REPLIES_MAX_ITEMS)},
        }
    )
    r = heal_op_line(line, _ctx(known_ids={"root"}))
    assert r.drop is False
    assert not any(n.startswith("clamped_quickreplies_items") for n in r.notes)


# ---------------------------------------------------------------------------
# Scheme-less URL coercion (checkout Handoff resilience)
# ---------------------------------------------------------------------------


def test_healer_coerces_scheme_less_handoff_url():
    """Bare-domain checkout Handoff url → https:// (and then validates)."""
    import json

    line = json.dumps(
        {
            "op": "add",
            "id": "root",
            "type": "Handoff",
            "props": {
                "reason": "checkout",
                "label": "Review and checkout",
                "url": "shop.myshopify.com/cart",
                "lifecycle": "popup",
            },
        }
    )
    r = heal_op_line(line, _ctx(known_ids=set()))
    assert r.drop is False
    assert any(n == "coerced_scheme_less_url:1" for n in r.notes)
    out = json.loads(r.line)  # type: ignore[arg-type]
    assert out["props"]["url"] == "https://shop.myshopify.com/cart"
    validate_props("Handoff", out["props"])


def test_healer_leaves_absolute_url_untouched():
    import json

    line = json.dumps(
        {
            "op": "add",
            "id": "root",
            "type": "Handoff",
            "props": {
                "reason": "checkout",
                "label": "Go",
                "url": "https://shop.com/cart",
                "lifecycle": "popup",
            },
        }
    )
    r = heal_op_line(line, _ctx(known_ids=set()))
    assert not any(n.startswith("coerced_scheme_less_url") for n in r.notes)
    out = json.loads(r.line)  # type: ignore[arg-type]
    assert out["props"]["url"] == "https://shop.com/cart"


def test_healer_leaves_hostless_relative_url():
    """A leading-slash path has no host to attach a scheme to — leave it for
    the validator (the healer must not invent a host)."""
    import json

    line = json.dumps(
        {
            "op": "add",
            "id": "root",
            "type": "Handoff",
            "props": {
                "reason": "checkout",
                "label": "Go",
                "url": "/cart",
                "lifecycle": "popup",
            },
        }
    )
    r = heal_op_line(line, _ctx(known_ids=set()))
    assert not any(n.startswith("coerced_scheme_less_url") for n in r.notes)
    out = json.loads(r.line)  # type: ignore[arg-type]
    assert out["props"]["url"] == "/cart"


def test_healer_leaves_mailto_untouched():
    import json

    line = json.dumps(
        {
            "op": "add",
            "id": "root",
            "type": "Handoff",
            "props": {
                "reason": "support",
                "label": "Email",
                "url": "mailto:a@b.com",
                "lifecycle": "popup",
            },
        }
    )
    r = heal_op_line(line, _ctx(known_ids=set()))
    assert not any(n.startswith("coerced_scheme_less_url") for n in r.notes)
    out = json.loads(r.line)  # type: ignore[arg-type]
    assert out["props"]["url"] == "mailto:a@b.com"


def test_healer_coerces_protocol_relative_image_src():
    import json

    line = json.dumps(
        {
            "op": "add",
            "id": "img",
            "type": "Image",
            "parent": "root",
            "props": {"src": "//cdn.shop.com/a.jpg", "alt": "a"},
        }
    )
    r = heal_op_line(line, _ctx(known_ids={"root"}))
    assert any(n == "coerced_scheme_less_url:1" for n in r.notes)
    out = json.loads(r.line)  # type: ignore[arg-type]
    assert out["props"]["src"] == "https://cdn.shop.com/a.jpg"


def test_healer_coerces_nested_quickreply_action_url():
    """Scheme-less url nested inside a QuickReplies item's open_url action."""
    import json

    line = json.dumps(
        {
            "op": "add",
            "id": "qr",
            "type": "QuickReplies",
            "parent": "root",
            "props": {
                "items": [
                    {
                        "label": "Track order",
                        "action": {"type": "open_url", "url": "shop.com/orders"},
                    },
                    {"label": "Home"},
                ]
            },
        }
    )
    r = heal_op_line(line, _ctx(known_ids={"root"}))
    assert any(n == "coerced_scheme_less_url:1" for n in r.notes)
    out = json.loads(r.line)  # type: ignore[arg-type]
    assert out["props"]["items"][0]["action"]["url"] == "https://shop.com/orders"
    validate_props("QuickReplies", out["props"])


def test_healer_leaves_sideeffect_relative_url_untouched():
    """``SideEffect.url`` is a same-origin-relative str (not HttpUrl) — a
    root-level dotted path like ``cart.js`` must NOT be rewritten into a host."""
    import json

    line = json.dumps(
        {
            "op": "add",
            "id": "se",
            "type": "SideEffect",
            "parent": "root",
            "props": {"kind": "fetch", "url": "cart.js", "method": "GET"},
        }
    )
    r = heal_op_line(line, _ctx(known_ids={"root"}))
    assert r.drop is False
    assert not any(n.startswith("coerced_scheme_less_url") for n in r.notes)
    out = json.loads(r.line)  # type: ignore[arg-type]
    assert out["props"]["url"] == "cart.js"

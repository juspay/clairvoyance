# pyrefly: ignore-errors
# Pydantic dynamic-attribute noise: validate_props("Tile", ...) returns
# _CatalogBase per its signature; pyrefly can't narrow on string dispatch,
# so concrete subclass attrs (.title, .body, .actions, etc.) read as
# missing-attribute even though runtime resolves them fine.
"""Tests for the Tile composite primitive's Pydantic schema.

Covers:
  * Full-fixture happy path covering every slot.
  * Required ``title`` enforcement.
  * Polymorphic ``body`` items — each ``kind`` validates the matching field.
  * Minimal happy case (title + one money body row).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.template.ui_catalog import validate_props

# ---------------------------------------------------------------------------
# Happy path: full Tile shape
# ---------------------------------------------------------------------------


def test_validate_full_tile_with_all_slots():
    props = {
        "media": {
            "src": "https://cdn.example.com/snowboard.jpg",
            "alt": "snowboard",
            "aspect": "1:1",
        },
        "eyebrow": "NEW",
        "title": "The Complete Snowboard",
        "subtitle": "Limited edition release",
        "body": [
            {"kind": "text", "text": "Hand-built with sustainable materials."},
            {"kind": "key_value", "key": "Price", "value": "₹699.95"},
            {"kind": "key_value", "key": "Material", "value": "Wood + epoxy"},
            {
                "kind": "message",
                "message": {
                    "severity": "warning",
                    "resolution": "recoverable",
                    "content": "Low stock",
                },
            },
        ],
        "attributes": [
            {"label": "Premium", "tone": "info"},
            {"label": "Eco", "tone": "positive"},
        ],
        "actions": [
            {
                "label": "Add to cart",
                "action": {"type": "to_assistant", "msg": "Add board to cart"},
                "variant": "primary",
            }
        ],
        "density": "spacious",
    }
    tile = validate_props("Tile", props)
    assert tile.title == "The Complete Snowboard"
    assert tile.density == "spacious"
    assert len(tile.body) == 4
    assert tile.body[1].value == "₹699.95"
    assert tile.body[2].key == "Material"
    assert len(tile.attributes) == 2
    assert tile.actions[0].label == "Add to cart"


# ---------------------------------------------------------------------------
# Required-field enforcement
# ---------------------------------------------------------------------------


def test_validate_tile_rejects_missing_title():
    """``title`` is the only required slot on a Tile."""
    props = {
        "body": [{"kind": "text", "text": "no title here"}],
    }
    with pytest.raises(ValidationError) as exc_info:
        validate_props("Tile", props)
    # The error must reference the missing title field.
    assert "title" in str(exc_info.value).lower()


def test_validate_tile_rejects_empty_title():
    """``min_length=1`` rejects an empty string just like missing."""
    with pytest.raises(ValidationError):
        validate_props("Tile", {"title": ""})


# ---------------------------------------------------------------------------
# Minimal happy case
# ---------------------------------------------------------------------------


def test_validate_minimal_tile_title_plus_key_value_body():
    """The shape canonical.template.json's search_catalog instruction tells
    the LLM to emit — title + one key_value body row, nothing else."""
    tile = validate_props(
        "Tile",
        {
            "title": "Foo",
            "body": [{"kind": "key_value", "key": "Price", "value": "₹699.95"}],
        },
    )
    assert tile.title == "Foo"
    assert tile.body[0].kind.value == "key_value"
    assert tile.body[0].key == "Price"
    assert tile.body[0].value == "₹699.95"
    # Density defaults to "default" — not required at the wire layer.
    assert tile.density == "default"


def test_validate_minimal_tile_just_title():
    """Title alone is enough — every other slot is optional."""
    tile = validate_props("Tile", {"title": "Bare"})
    assert tile.title == "Bare"
    assert tile.body == []
    assert tile.attributes == []
    assert tile.actions == []
    assert tile.media is None


# ---------------------------------------------------------------------------
# Polymorphic body items — each kind validates correctly
# ---------------------------------------------------------------------------


def test_body_kind_text():
    tile = validate_props(
        "Tile",
        {
            "title": "T",
            "body": [{"kind": "text", "text": "A paragraph of body copy."}],
        },
    )
    assert tile.body[0].text == "A paragraph of body copy."


def test_body_kind_key_value():
    tile = validate_props(
        "Tile",
        {
            "title": "T",
            "body": [{"kind": "key_value", "key": "Color", "value": "Cobalt blue"}],
        },
    )
    assert tile.body[0].key == "Color"
    assert tile.body[0].value == "Cobalt blue"


def test_body_kind_message():
    tile = validate_props(
        "Tile",
        {
            "title": "T",
            "body": [
                {
                    "kind": "message",
                    "message": {
                        "severity": "info",
                        "resolution": "recoverable",
                        "content": "Heads up: low stock.",
                    },
                }
            ],
        },
    )
    assert tile.body[0].message.content == "Heads up: low stock."


def test_body_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        validate_props(
            "Tile",
            {
                "title": "T",
                "body": [{"kind": "bogus", "text": "x"}],
            },
        )


def test_body_rejects_extra_props_inside_item():
    """``extra='forbid'`` on TileBodyItem catches typos / hallucinated fields."""
    with pytest.raises(ValidationError):
        validate_props(
            "Tile",
            {
                "title": "T",
                "body": [
                    {
                        "kind": "text",
                        "text": "hi",
                        "weight": "bold",  # not a valid TileBodyItem field
                    }
                ],
            },
        )


def test_tile_rejects_unknown_top_level_prop():
    """The renderer healer relies on ``extra='forbid'`` to strip noise."""
    with pytest.raises(ValidationError):
        validate_props(
            "Tile",
            {
                "title": "T",
                "footnote": "unsupported slot",
            },
        )


# ---------------------------------------------------------------------------
# Action variant validation
# ---------------------------------------------------------------------------


def test_tile_action_to_assistant():
    tile = validate_props(
        "Tile",
        {
            "title": "T",
            "actions": [
                {
                    "label": "View",
                    "action": {"type": "to_assistant", "msg": "Tell me about T"},
                }
            ],
        },
    )
    assert tile.actions[0].action.msg == "Tell me about T"
    # variant defaults to "primary"
    assert tile.actions[0].variant == "primary"


def test_tile_action_open_url():
    tile = validate_props(
        "Tile",
        {
            "title": "T",
            "actions": [
                {
                    "label": "Docs",
                    "action": {"type": "open_url", "url": "https://example.com/"},
                    "variant": "ghost",
                }
            ],
        },
    )
    assert tile.actions[0].variant == "ghost"

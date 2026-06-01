"""Tests for the per-template "## Available primitives" renderer.

The renderer walks the catalog's curated render order, drops names not
in the session's allowlist, and emits a Markdown section the chat agent
splices into the system prompt. These tests pin the load-bearing
behaviours: section header / footer presence, per-primitive entries,
allowlist filtering, and the empty-state fallback.
"""

from __future__ import annotations

import json

# Load the template package before any chat/* module reaches it — same
# circular-import precaution as the sibling ui_stream / ui_healer tests.
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (  # noqa: F401
    PRIMITIVE_RENDER_ORDER,
    UI_CATALOG,
    validate_props,
)
from app.ai.voice.agents.breeze_buddy.template.ui_prompt import (
    render_primitives_section,
)

# ---------------------------------------------------------------------------
# Allowlist filtering
# ---------------------------------------------------------------------------


def test_section_includes_only_allowlisted_primitives():
    section = render_primitives_section({"Tile", "Carousel", "Tag"})
    # Headings appear for each allowlisted primitive.
    assert "**Tile**" in section
    assert "**Carousel**" in section
    assert "**Tag**" in section
    # And do NOT appear for primitives outside the allowlist. We check
    # the bold-marked header form to avoid false positives on prose
    # mentions (the footer mentions Tile/Card/Image/Text by name).
    assert "**Stack**" not in section
    assert "**Card**" not in section
    assert "**Image**" not in section
    assert "**Text**" not in section
    assert "**Table**" not in section
    assert "**Message**" not in section
    assert "**Handoff**" not in section
    assert "**Button**" not in section


def test_section_renders_in_curated_order():
    # Tile is rendered before Carousel before Tag per PRIMITIVE_RENDER_ORDER.
    section = render_primitives_section({"Tag", "Carousel", "Tile"})
    tile_idx = section.index("**Tile**")
    carousel_idx = section.index("**Carousel**")
    tag_idx = section.index("**Tag**")
    assert tile_idx < carousel_idx < tag_idx


# ---------------------------------------------------------------------------
# Tile entry — slot names matter for the JIT pattern
# ---------------------------------------------------------------------------


def test_tile_entry_mentions_all_slot_names():
    section = render_primitives_section({"Tile"})
    # The Tile renderer relies on these slot names being visible to the
    # LLM. Pin them — if a future schema renames a slot, this test
    # forces the prompt rendering to catch up.
    for slot in (
        "media",
        "eyebrow",
        "title",
        "subtitle",
        "body",
        "attributes",
        "actions",
    ):
        assert slot in section, f"missing Tile slot: {slot}"


def test_tile_entry_marks_title_required():
    section = render_primitives_section({"Tile"})
    # ``title*`` with the asterisk marker.
    assert "title*:" in section


def test_tile_entry_marks_media_optional():
    section = render_primitives_section({"Tile"})
    assert "media?:" in section


def test_tile_example_validates_against_catalog():
    """The hard-coded compact example must round-trip through
    expand_compact_op -> validate_props cleanly, otherwise we'd be teaching
    the LLM an invalid shape."""
    # Local import: the chat package must load after the template package
    # (circular-import precaution above), so don't hoist this to module top.
    from app.ai.voice.agents.breeze_buddy.chat.ui_stream import expand_compact_op

    section = render_primitives_section({"Tile"})
    # Pull the JSON after the "Example: " marker for the Tile entry.
    tile_block = section.split("**Tile**", 1)[1]
    example_line = next(
        line for line in tile_block.splitlines() if line.strip().startswith("Example:")
    )
    # Example is in compact wire form — expand to canonical before validating.
    payload = expand_compact_op(
        json.loads(example_line.split("Example:", 1)[1].strip())
    )
    assert payload["type"] == "Tile"
    # Server-side validator must accept the expanded example props.
    validate_props("Tile", payload["props"])


# ---------------------------------------------------------------------------
# Section frame — header, footer, composition rules
# ---------------------------------------------------------------------------


def test_section_starts_with_canonical_header():
    section = render_primitives_section({"Tile"})
    assert section.startswith("## Available primitives")
    # Header explains the asterisk convention so the LLM understands.
    assert "Asterisk (*) marks required props" in section


def test_section_has_action_shape_block():
    section = render_primitives_section({"Tile"})
    assert "Action shape" in section
    assert "to_assistant" in section
    assert "open_url" in section


def test_section_has_composition_rules_at_bottom():
    section = render_primitives_section({"Tile"})
    rules_idx = section.find("Composition rules:")
    assert rules_idx != -1
    # The composition rules block is the last thing — nothing else
    # follows it of any consequence.
    tail = section[rules_idx:]
    assert "Root id is always" in tail
    assert "non-root `add` ops MUST have `parent`" in tail
    assert "ONE Tile per item" in tail


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_allowlist_does_not_crash():
    section = render_primitives_section(set())
    # Header + footer still rendered so the LLM gets a coherent message.
    assert "## Available primitives" in section
    assert "Composition rules:" in section
    # And the empty-state body is present.
    assert "No primitives are enabled" in section


def test_unknown_primitive_in_allowlist_is_ignored():
    # Mixing in a name that's not in PRIMITIVE_RENDER_ORDER / UI_CATALOG
    # should be silently skipped (defence against stale templates).
    section = render_primitives_section({"Tile", "NotARealPrimitive"})
    assert "**Tile**" in section
    assert "NotARealPrimitive" not in section

"""Tests for template-level primitive allowlist filtering in ``parse_op_line``.

The resolved per-template allowlist (see ``ui_catalog.resolve_allowlist`` +
``UiCatalogConfig``) drops ``add`` ops whose type is known to the catalog
but NOT enabled for the template. The error reason must be distinct from
``unknown_type`` so telemetry separates "LLM hallucinated" from
"merchant turned off".
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.chat.ui.stream import parse_op_line

# Load template package first to avoid circular-import trap on first import
# of chat/* (mirrors test_ui_stream + test_session_state).
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (  # noqa: F401
    UI_CATALOG,
)

# ---------------------------------------------------------------------------
# Disabled primitives drop with the distinct reason
# ---------------------------------------------------------------------------


def test_disabled_known_primitive_yields_primitive_disabled_error():
    """Carousel is in the catalog, but allowlist only contains Tile —
    so the op must drop with ``primitive_disabled:Carousel``."""
    line = '{"op":"add","id":"root","type":"Carousel"}'
    r = parse_op_line(line, allowlist={"Tile"})
    assert r.op is None
    assert r.error == "primitive_disabled:Carousel"


def test_primitive_disabled_is_distinct_from_unknown_type():
    """An unknown type with allowlist still set must surface unknown_type,
    NOT primitive_disabled — so telemetry can tell the two cases apart."""
    line = '{"op":"add","id":"x","type":"NotAPrimitive","parent":"root"}'
    r = parse_op_line(line, allowlist={"Tile"})
    assert r.op is None
    assert r.error and r.error.startswith("unknown_type")
    assert "primitive_disabled" not in r.error


def test_allowlist_none_means_no_template_level_filtering():
    """When ``allowlist=None``, every catalog-registered type passes
    (back-compat for pre-ui_catalog templates)."""
    line = '{"op":"add","id":"root","type":"Carousel"}'
    r = parse_op_line(line, allowlist=None)
    assert r.error is None
    assert r.op is not None
    assert r.op["type"] == "Carousel"


def test_empty_allowlist_disables_everything():
    """``allowlist=set()`` (vs ``None``) explicitly disables every primitive —
    every add op drops with ``primitive_disabled:<type>``."""
    line = '{"op":"add","id":"root","type":"Carousel"}'
    r = parse_op_line(line, allowlist=set())
    assert r.op is None
    assert r.error == "primitive_disabled:Carousel"

    line2 = '{"op":"add","id":"root","type":"Tile","props":{"title":"x"}}'
    r2 = parse_op_line(line2, allowlist=set())
    assert r2.op is None
    assert r2.error == "primitive_disabled:Tile"


def test_allowlist_pass_when_type_enabled():
    """When the type IS in the allowlist, normal validation continues."""
    line = '{"op":"add","id":"root","type":"Tile","props":{"title":"hello"}}'
    r = parse_op_line(line, allowlist={"Tile"})
    assert r.error is None
    assert r.op is not None
    assert r.op["type"] == "Tile"
    assert r.op["props"]["title"] == "hello"


# ---------------------------------------------------------------------------
# Allowlist applies to add only — remove/replace pass through
# ---------------------------------------------------------------------------


def test_remove_op_ignores_allowlist():
    """``remove`` carries only id — there is no type to gate."""
    line = '{"op":"remove","id":"c1"}'
    r = parse_op_line(line, allowlist=set())
    assert r.error is None
    assert r.op == {"op": "remove", "id": "c1"}


def test_replace_op_ignores_allowlist_when_no_type_specified():
    """``replace`` carries new props only; type comes from existing tree state
    at apply time on the widget. Allowlist only gates ``add``."""
    line = '{"op":"replace","id":"c1","props":{"text":"hi"}}'
    r = parse_op_line(line, allowlist=set())
    assert r.error is None
    assert r.op is not None
    assert r.op["op"] == "replace"

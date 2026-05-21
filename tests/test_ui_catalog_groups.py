"""Tests for the UI-catalog group registry + allowlist resolver.

Covers:
  * ``resolve_allowlist`` — default ("core" only), groups + primitives mix,
    disabled overrides, unknown group/primitive tolerance.
  * ``group_for`` — primitive → group reverse lookup.
"""

from __future__ import annotations

# Load template package first so its __init__ chain completes before any
# `chat/*` module imports (mirrors test_ui_stream / test_session_state).
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    PRIMITIVE_GROUPS,
    UI_CATALOG,
    group_for,
    resolve_allowlist,
)

# ---------------------------------------------------------------------------
# resolve_allowlist
# ---------------------------------------------------------------------------


def test_resolve_default_returns_core_group_only():
    """When called with no args, defaults to just the 'core' group — the
    backward-compat behaviour for templates predating ``ui_catalog``."""
    result = resolve_allowlist()
    expected = set(PRIMITIVE_GROUPS["core"])
    assert result == expected
    # Composite primitives (e.g. Tile) must NOT leak in.
    assert "Tile" not in result


def test_resolve_groups_core_plus_composite_includes_tile():
    result = resolve_allowlist(enabled_groups=["core", "composite"])
    assert "Tile" in result
    # Sanity: still has all core primitives.
    for prim in PRIMITIVE_GROUPS["core"]:
        assert prim in result


def test_resolve_disabled_primitive_subtracts_from_group():
    """``disabled_primitives`` wins over ``enabled_groups``."""
    result = resolve_allowlist(enabled_groups=["core"], disabled_primitives=["Table"])
    assert "Table" not in result
    # Everything else core remains.
    for prim in PRIMITIVE_GROUPS["core"]:
        if prim == "Table":
            continue
        assert prim in result


def test_resolve_enabled_primitive_without_group():
    """``enabled_primitives`` adds one-offs even when no group is enabled."""
    result = resolve_allowlist(enabled_primitives=["Tile"])
    assert result == {"Tile"}


def test_resolve_unknown_group_is_silently_dropped():
    """Stale templates referencing a removed group don't crash a session."""
    result = resolve_allowlist(enabled_groups=["nonexistent"])
    assert result == set()


def test_resolve_unknown_primitive_in_enabled_is_dropped():
    """Stale templates referencing a removed primitive don't crash either."""
    result = resolve_allowlist(enabled_primitives=["NotAPrimitive", "Tile"])
    assert result == {"Tile"}


def test_resolve_combined_groups_and_primitives_with_disabled():
    """Full precedence walk — groups expand, primitives union, disabled subtract."""
    result = resolve_allowlist(
        enabled_groups=["core"],
        enabled_primitives=["Tile"],
        disabled_primitives=["Table", "Message"],
    )
    assert "Tile" in result  # added by enabled_primitives
    assert "Table" not in result  # subtracted
    assert "Message" not in result  # subtracted
    assert "Stack" in result  # core, not subtracted


# ---------------------------------------------------------------------------
# group_for
# ---------------------------------------------------------------------------


def test_group_for_composite_returns_composite():
    assert group_for("Tile") == "composite"


def test_group_for_core_primitives_returns_core():
    assert group_for("Text") == "core"
    assert group_for("Stack") == "core"
    assert group_for("Carousel") == "core"


def test_money_is_not_a_registered_primitive():
    """Money was deleted as a commerce leak — the runtime ships zero
    commerce-tinted primitives."""
    assert "Money" not in UI_CATALOG
    assert group_for("Money") is None


def test_group_for_unknown_returns_none():
    assert group_for("Foo") is None
    assert group_for("ProductCarousel") is None


# ---------------------------------------------------------------------------
# Catalog x group registry consistency
# ---------------------------------------------------------------------------


def test_every_group_member_is_registered_in_catalog():
    """No group should reference a primitive that isn't in UI_CATALOG."""
    catalog_keys = set(UI_CATALOG.keys())
    for group_name, members in PRIMITIVE_GROUPS.items():
        for prim in members:
            assert prim in catalog_keys, (
                f"group {group_name!r} references {prim!r} "
                f"which is not registered in UI_CATALOG"
            )

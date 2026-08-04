"""Tests for the UI-catalog group registry + allowlist resolver.

Covers:
  * ``resolve_allowlist`` — default ("core" only), groups + primitives mix,
    disabled overrides, unknown group/primitive tolerance.
  * ``group_for`` — primitive → group reverse lookup.
  * ``graphs`` group enablement + chart primitive ``validate_props`` shape.
  * ``metrics`` group enablement + metric primitive ``validate_props`` shape.
"""

from __future__ import annotations

# Load template package first so its __init__ chain completes before any
# `chat/*` module imports (mirrors test_ui_stream / test_session_state).
import pytest
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    PRIMITIVE_GROUPS,
    UI_CATALOG,
    SideEffect,
    SideEffectKind,
    group_for,
    resolve_allowlist,
    validate_props,
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


# ---------------------------------------------------------------------------
# SideEffect — kind-specific required fields enforced at validation time
# ---------------------------------------------------------------------------


def test_sideeffect_fetch_requires_url():
    """``kind='fetch'`` without a non-empty ``url`` is invalid — the widget
    would silently no-op, but the contract should reject at parse time."""
    with pytest.raises(ValidationError, match="kind='fetch' requires"):
        SideEffect(kind=SideEffectKind.fetch)
    with pytest.raises(ValidationError, match="kind='fetch' requires"):
        SideEffect(kind=SideEffectKind.fetch, url="")


def test_sideeffect_fetch_with_url_is_valid():
    op = SideEffect(kind=SideEffectKind.fetch, url="/cart/sync")
    assert op.url == "/cart/sync"
    assert op.method == "GET"
    assert op.credentials == "include"


def test_sideeffect_set_cookie_requires_name_and_value():
    with pytest.raises(ValidationError, match="kind='set_cookie' requires"):
        SideEffect(kind=SideEffectKind.set_cookie)
    with pytest.raises(ValidationError, match="kind='set_cookie' requires.*name"):
        SideEffect(kind=SideEffectKind.set_cookie, value="abc")
    with pytest.raises(ValidationError, match="kind='set_cookie' requires.*value"):
        SideEffect(kind=SideEffectKind.set_cookie, name="cart")
    with pytest.raises(ValidationError, match="kind='set_cookie' requires.*name"):
        SideEffect(kind=SideEffectKind.set_cookie, name="", value="abc")
    with pytest.raises(ValidationError, match="kind='set_cookie' requires.*value"):
        SideEffect(kind=SideEffectKind.set_cookie, name="cart", value="")


def test_sideeffect_set_cookie_with_name_and_value_is_valid():
    op = SideEffect(kind=SideEffectKind.set_cookie, name="cart", value="abc123")
    assert op.name == "cart"
    assert op.value == "abc123"
    assert op.path == "/"
    assert op.samesite == "Lax"
    assert op.secure is True


def test_sideeffect_default_kind_is_fetch_so_url_required():
    """``kind`` defaults to ``fetch`` — a bare ``SideEffect()`` must reject
    for the same reason ``SideEffect(kind=fetch)`` does."""
    with pytest.raises(ValidationError, match="kind='fetch' requires"):
        SideEffect()


# ---------------------------------------------------------------------------
# graphs group
# ---------------------------------------------------------------------------


def test_resolve_graphs_group_includes_all_chart_types():
    """Enabling the ``graphs`` group surfaces exactly the four chart types."""
    result = resolve_allowlist(enabled_groups=["graphs"])
    assert result == {"BarChart", "LineChart", "AreaChart", "PieChart"}


def test_chart_primitives_map_back_to_graphs_group():
    """Each chart primitive reverse-maps to the ``graphs`` group."""
    for chart in ("BarChart", "LineChart", "AreaChart", "PieChart"):
        assert group_for(chart) == "graphs"


def test_validate_props_accepts_minimal_bar_chart():
    """A minimal single-datum BarChart payload passes validation."""
    chart = validate_props("BarChart", {"data": [{"label": "Mon", "value": 5}]})
    # validate_props is typed to return the _CatalogBase base, so read the
    # concrete fields via model_dump() rather than dynamic attribute access.
    datum = chart.model_dump()["data"][0]
    assert datum["label"] == "Mon"
    assert datum["value"] == 5


def test_validate_props_rejects_empty_chart_datum_label():
    """``ChartPoint.label`` enforces ``min_length=1`` — empty labels are rejected
    (parity with Button/Tag/Table labels)."""
    with pytest.raises(ValidationError):
        validate_props("BarChart", {"data": [{"label": "", "value": 5}]})


# ---------------------------------------------------------------------------
# metrics group
# ---------------------------------------------------------------------------


def test_resolve_metrics_group_includes_all_metric_types():
    """Enabling the ``metrics`` group surfaces exactly the four metric types."""
    result = resolve_allowlist(enabled_groups=["metrics"])
    assert result == {"KPI", "MetricCard", "Sparkline", "ProgressBar"}


def test_metric_primitives_map_back_to_metrics_group():
    """Each metric primitive reverse-maps to the ``metrics`` group."""
    for prim in ("KPI", "MetricCard", "Sparkline", "ProgressBar"):
        assert group_for(prim) == "metrics"


def test_validate_props_accepts_minimal_kpi():
    """A KPI with just label + value validates and round-trips its fields."""
    kpi = validate_props("KPI", {"label": "Total calls", "value": "2,999"})
    dumped = kpi.model_dump()
    assert dumped["label"] == "Total calls"
    assert dumped["value"] == "2,999"


def test_validate_props_rejects_empty_kpi_label():
    """``KPI.label`` enforces ``min_length=1`` — empty labels are rejected
    so the widget never renders a blank metric label."""
    with pytest.raises(ValidationError):
        validate_props("KPI", {"label": "", "value": "42"})


# ---------------------------------------------------------------------------
# Lazy flavor-group loading — fail-open
# ---------------------------------------------------------------------------


def test_lazy_group_with_broken_module_degrades_to_core(monkeypatch):
    """A flavor group whose module is absent (its layer hasn't shipped yet)
    or raises on import must leave the catalog untouched and resolve to the
    core allowlist — never surface the ImportError as a 500 from whichever
    endpoint happened to touch the group.
    """
    import app.ai.voice.agents.breeze_buddy.template.ui_catalog as uc

    monkeypatch.setitem(uc.LAZY_GROUPS, "brokenflavor", "no.such.module.anywhere")
    monkeypatch.setattr(uc, "_LOADED_LAZY_GROUPS", set())

    uc.ensure_group_loaded("brokenflavor")  # must not raise

    allowlist = resolve_allowlist(enabled_groups=["core", "brokenflavor"])
    assert allowlist == resolve_allowlist(enabled_groups=["core"])


def test_failed_lazy_group_import_is_not_retried(monkeypatch):
    """Python drops a failed module from sys.modules; without marking the
    attempt, every request would re-import and re-raise."""
    import app.ai.voice.agents.breeze_buddy.template.ui_catalog as uc

    attempts = {"n": 0}

    def _explode(_path):
        attempts["n"] += 1
        raise RuntimeError("flavor module is broken")

    monkeypatch.setitem(uc.LAZY_GROUPS, "brokenflavor", "no.such.module.anywhere")
    monkeypatch.setattr(uc, "_LOADED_LAZY_GROUPS", set())
    monkeypatch.setattr(uc.importlib, "import_module", _explode)

    for _ in range(3):
        uc.ensure_group_loaded("brokenflavor")
    assert attempts["n"] == 1

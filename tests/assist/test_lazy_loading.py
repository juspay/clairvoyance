"""Lazy flavor loading — the two-way conditional-load guarantee.

(a) A process that only ever resolves core templates must never import
    ``assist.commerce`` (asserted in a subprocess so no sibling test's
    load leaks in), and loading commerce later must not change the
    core-only prompt section (lru_cache-key stability: loading only ever
    ADDS types; a core allowlist never names them).
(b) Enabling the group loads + registers the components; the loaders are
    idempotent; intents from a non-enabled flavor 422 with a typed error.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai.voice.agents.breeze_buddy.chat.intents import router as ir
from app.ai.voice.agents.breeze_buddy.template import ui_catalog as uc
from app.ai.voice.agents.breeze_buddy.template.types import UiCatalogConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]

COMMERCE = {"ProductGrid", "ProductCard", "CartView", "ProductDetail", "OrderStatus"}

# Runs a full core-only session surface (ChatAgent init → allowlist →
# prompt section render) and asserts no assist module was imported; then
# enables commerce IN THE SAME PROCESS and asserts (1) the module loads,
# (2) the core-only section render is byte-identical to the pre-load one.
_CORE_ONLY_SCRIPT = """
import sys
from types import SimpleNamespace

from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent
from app.ai.voice.agents.breeze_buddy.template.ui_prompt import (
    render_primitives_section,
)
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import resolve_allowlist

template = SimpleNamespace(id="t-core", flow=None, configurations=None)
agent = ChatAgent(
    session_id="s-core", template=template, llm=None, catalog_version="v2"
)
section_before = render_primitives_section(agent.ui_allowlist)
assert "ProductGrid" not in section_before
assert "### Data-bound components" not in section_before

_ASSIST_PREFIX = "app.ai.voice.agents.breeze_buddy.assist"
assist_modules = [m for m in sys.modules if m.startswith(_ASSIST_PREFIX)]
assert not assist_modules, f"assist leaked into a core-only process: {assist_modules}"

# Now a commerce template shows up in the same process: the flavor loads,
# and the ALREADY-CACHED core section stays byte-identical (loading only
# ever adds types; the core allowlist never contains commerce names).
allow = resolve_allowlist(enabled_groups=["core", "commerce"])
assert "app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas" in sys.modules
assert {"ProductGrid", "ProductCard", "CartView", "ProductDetail"} <= allow
section_after = render_primitives_section(
    ChatAgent(
        session_id="s-core-2", template=template, llm=None, catalog_version="v2"
    ).ui_allowlist
)
assert section_after == section_before, "core prompt section changed after flavor load"
print("OK")
"""


def test_core_only_process_never_imports_commerce():
    proc = subprocess.run(
        [sys.executable, "-c", _CORE_ONLY_SCRIPT],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "OK" in proc.stdout


# ---------------------------------------------------------------------------
# Load-on-enable + idempotency (in-process; loading may already have
# happened via a sibling test — these assert converged end state only)
# ---------------------------------------------------------------------------


def test_enabling_commerce_loads_and_registers():
    allow = uc.resolve_allowlist(enabled_groups=["core", "commerce"])
    assert COMMERCE <= allow
    assert set(uc.PRIMITIVE_GROUPS["commerce"]) == COMMERCE
    assert uc.is_known_type("CartView")
    assert uc.is_data_bound("ProductGrid")
    module = uc.LAZY_GROUPS["commerce"]
    assert module in sys.modules


def test_commerce_render_ui_prompt_registers_with_group_load():
    # The flavor's render_ui vocabulary rides the same lazy hook as the
    # schemas: once the group is loaded, prompt splicing for a
    # commerce-enabled template gets the shopper copy, not the generic
    # engine contract.
    from app.ai.voice.agents.breeze_buddy.template.ui_prompt import (
        render_render_ui_section,
    )

    uc.ensure_group_loaded("commerce")
    section = render_render_ui_section("forced_final", ["commerce"])
    assert "You show shoppers products, carts, and quick replies" in section
    assert "(Add to cart, View, a checkout button)" in section
    assert "<plan>" in section  # engine-owned plan instruction still rides
    # And a template without the group still gets the generic contract.
    generic = render_render_ui_section("forced_final", ["core"])
    assert "shopper" not in generic


def test_commerce_render_ui_pack_registers_with_group_load():
    # The render_ui surface pack (schema vocabulary + summarizer +
    # projection policy) rides the same lazy hook as the schemas.
    from app.ai.voice.agents.breeze_buddy.chat.ui.render_ui_tool import (
        build_render_ui_schema,
    )

    uc.ensure_group_loaded("commerce")
    schema = build_render_ui_schema(
        ["ProductGrid"], handler=None, flavor_groups=["commerce"]
    )
    assert "shopper" in schema.description
    assert "ProductGrid selection" in schema.properties["items"]["description"]


def test_ensure_group_loaded_is_idempotent():
    uc.ensure_group_loaded("commerce")
    uc.ensure_group_loaded("commerce")
    assert [n for n in uc.PRIMITIVE_GROUPS["commerce"]].count("CartView") == 1
    assert uc.PRIMITIVE_RENDER_ORDER.count("ProductGrid") == 1
    # Re-registration must not duplicate either.
    import app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas as cs

    uc.register_primitives(
        "commerce", {"CartView": cs.CartView}, render_order=["CartView"]
    )
    assert uc.PRIMITIVE_RENDER_ORDER.count("CartView") == 1
    assert uc.PRIMITIVE_GROUPS["commerce"].count("CartView") == 1


def test_unknown_group_is_a_noop():
    before = set(uc.UI_CATALOG)
    uc.ensure_group_loaded("healthcare")  # not a lazy group — ignored
    assert set(uc.UI_CATALOG) == before


# ---------------------------------------------------------------------------
# Intent flavor gating — typed 422s for sessions without the flavor
# ---------------------------------------------------------------------------


def _wire(intent: str) -> dict:
    return {
        "intent": intent,
        "component_id": "pg1",
        "payload": {"variant_id": "v1"},
    }


def test_intent_from_unenabled_flavor_is_typed_422():
    ir.ensure_flavor_intents(["commerce"])
    with pytest.raises(ir.IntentValidationError) as exc:
        ir.parse_ui_intent(_wire("add_to_cart"), enabled_flavors={"core"})
    assert exc.value.detail["code"] == "flavor_not_enabled"


def test_intent_passes_when_flavor_enabled():
    ir.ensure_flavor_intents(["commerce"])
    parsed = ir.parse_ui_intent(
        _wire("add_to_cart"), enabled_flavors={"core", "commerce"}
    )
    assert parsed.policy.flavor == "commerce"
    assert parsed.policy.route is ir.IntentRoute.DIRECT


def test_unknown_intent_stays_unknown_regardless_of_flavors():
    with pytest.raises(ir.IntentValidationError) as exc:
        ir.parse_ui_intent(_wire("teleport"), enabled_flavors={"core", "commerce"})
    assert exc.value.detail["code"] == "unknown_intent"


def test_ensure_flavor_intents_ignores_unknown_flavors():
    ir.ensure_flavor_intents(["core", "no_such_flavor"])  # must not raise


# ---------------------------------------------------------------------------
# template_enabled_flavors — the scope handed to the intent gate
# ---------------------------------------------------------------------------


def test_template_enabled_flavors_defaults_to_core():
    assert ir.template_enabled_flavors(SimpleNamespace(configurations=None)) == {"core"}
    assert ir.template_enabled_flavors(
        SimpleNamespace(configurations=SimpleNamespace(ui_catalog=None))
    ) == {"core"}


def test_template_enabled_flavors_reads_enabled_groups():
    template = SimpleNamespace(
        configurations=SimpleNamespace(
            ui_catalog=UiCatalogConfig(enabled_groups=["core", "commerce"])
        )
    )
    assert ir.template_enabled_flavors(template) == {"core", "commerce"}

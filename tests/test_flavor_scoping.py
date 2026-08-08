"""Flavor registries are per-template, not per-process.

Every flavor registry is process-global and filled at lazy import time, so
the FIRST commerce template to warm a worker used to change tool behaviour
for every other merchant sharing that process — none of whom enabled the
flavor. This module pins both halves of the gate:

* **Group scope** — a session whose template did not enable ``commerce``
  sees engine defaults for the very same tool names, with the flavor fully
  loaded in the same process (which the import below guarantees).
* **Role binding** — a template that points a flavor ROLE at its own
  gateway's tool name keeps the metadata registered for that role.

The contamination tests deliberately assert on tool names commerce also
uses (``search_catalog``, ``get_cart``, ``update_cart``): overlapping names
across merchants are the realistic case, and the whole point is that the
overlap no longer matters.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai.voice.agents.breeze_buddy.chat.flavors import (
    EMPTY_SCOPE,
    resolve_flavor_scope,
    role_key,
)
from app.ai.voice.agents.breeze_buddy.chat.steps.labels import (
    resolve_step_label,
    summarize_step_result,
)
from app.ai.voice.agents.breeze_buddy.chat.steps.verification import run_tool_verifiers
from app.ai.voice.agents.breeze_buddy.chat.tools.annotations import (
    is_read_only,
    resolve_tool_annotation,
)
from app.ai.voice.agents.breeze_buddy.chat.tools.result_annotators import (
    run_result_annotators,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.binding import selector_extension_keys
from app.ai.voice.agents.breeze_buddy.chat.ui.render_ui_tool import (
    build_render_ui_schema,
)
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    UI_CATALOG,
    group_for,
    register_primitives,
)

# Warms the flavor into this process exactly like a live commerce session
# would — every assertion below runs with the registries POPULATED.
import app.ai.voice.agents.breeze_buddy.assist.commerce  # noqa: F401  isort: skip

_COMMERCE = resolve_flavor_scope(None, ["commerce"])


def _template(tools: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        configurations=SimpleNamespace(
            ui_intents=SimpleNamespace(tools=tools) if tools else None,
            tool_execution=None,
        )
    )


# ---------------------------------------------------------------------------
# Group scope — a non-commerce session is untouched by the loaded flavor
# ---------------------------------------------------------------------------


class TestNoCrossTemplateContamination:
    def test_step_labels_stay_generic(self):
        assert resolve_step_label("get_product", EMPTY_SCOPE) == (
            "Getting the product",
            "Got the product",
        )
        assert resolve_step_label("get_cart", EMPTY_SCOPE) == (
            "Getting the cart",
            "Got the cart",
        )

    def test_shape_keyed_summarizer_never_runs(self):
        # The dangerous one: it matches on payload SHAPE, so any tool
        # returning a ``products`` or ``line_items`` list would have been
        # captioned with commerce wording.
        assert summarize_step_result({"products": [{}, {}]}, EMPTY_SCOPE) == (
            None,
            None,
        )
        assert summarize_step_result({"line_items": [{}]}, EMPTY_SCOPE) == (None, None)

    def test_annotations_stay_destructive(self):
        # Annotations drive parallel fan-out and mutation serialization —
        # a leaked ``read_only`` would run a stranger's mutations
        # concurrently.
        assert resolve_tool_annotation("search_catalog", None, EMPTY_SCOPE) == (
            "destructive"
        )
        assert not is_read_only("get_cart", None, EMPTY_SCOPE)

    def test_verifiers_never_rewrite_a_success(self):
        # The worst failure mode: a SUCCESSFUL call turned into an error
        # envelope before the store, reducers and LLM ever see it, purely
        # because another merchant's flavor is warm in this worker.
        foreign_success = {"status": "success", "items": [{"sku": "a"}]}
        assert run_tool_verifiers("search_catalog", {}, foreign_success) is None
        assert (
            run_tool_verifiers("search_catalog", {}, foreign_success, _COMMERCE)
            == "search result carries no products array"
        )

    def test_result_annotators_do_not_stamp_foreign_results(self):
        result = {"products": [{"id": "p1", "title": "Anything"}]}
        assert run_result_annotators("search_catalog", {"query": "x"}, result) is result

    def test_selector_keys_stay_out_of_the_render_ui_schema(self):
        # These splice into the LLM-facing tool schema, so a leak changes
        # what every other merchant's model is told it may send.
        assert selector_extension_keys(None) == []
        assert selector_extension_keys(["core"]) == []
        assert selector_extension_keys(["commerce"]) == ["feature_variant"]

        core_schema = build_render_ui_schema(
            ["Table"], handler=None, flavor_groups=["core"]
        )
        assert set(core_schema.properties["items"]["items"]["properties"]) == {"id"}
        commerce_schema = build_render_ui_schema(
            ["ProductGrid"], handler=None, flavor_groups=["commerce"]
        )
        assert "feature_variant" in (
            commerce_schema.properties["items"]["items"]["properties"]
        )


# ---------------------------------------------------------------------------
# Role binding — metadata follows the tool the template actually calls
# ---------------------------------------------------------------------------


class TestRoleBinding:
    """A template may bind a flavor role to its own gateway's tool name via
    ``ui_intents.tools``. Before role binding, dispatch used the template's
    name while the registries were keyed on the flavor's default — so the
    merchant silently lost the verifier, got ``destructive`` (no fan-out)
    and a humanized step label."""

    REBOUND = {"search": "find_products", "get_cart": "fetch_basket"}

    @pytest.fixture
    def scope(self):
        return resolve_flavor_scope(_template(self.REBOUND), ["commerce"])

    def test_verifier_follows_the_rebound_tool(self, scope):
        assert (
            run_tool_verifiers("find_products", {}, {"status": "success"}, scope)
            == "search result carries no products array"
        )

    def test_annotation_follows_the_rebound_tool(self, scope):
        assert resolve_tool_annotation("find_products", None, scope) == "read_only"
        assert is_read_only("fetch_basket", None, scope)

    def test_step_label_follows_the_rebound_tool(self, scope):
        assert resolve_step_label("fetch_basket", scope) == (
            "Checking your cart",
            "Checked your cart",
        )

    def test_result_annotator_follows_the_rebound_tool(self, scope):
        annotated = run_result_annotators(
            "find_products",
            {"query": "leggings"},
            {"products": [{"id": "p1", "title": "Flowmesh Leggings"}]},
            scope,
        )
        assert annotated["products"][0].get("matched_via")

    def test_unbound_roles_keep_their_defaults(self, scope):
        # Only `search` and `get_cart` were rebound; the rest still match
        # on the UCP default names.
        assert resolve_tool_annotation("update_cart", None, scope) == "idempotent"
        assert resolve_step_label("get_product", scope) == (
            "Checking the product",
            "Checked the product",
        )

    def test_the_flavors_own_default_name_no_longer_matches(self, scope):
        # The flavor's default names are what THIS template bound
        # elsewhere, so they are just unknown tools here. Checked for both
        # a role whose name differs from its default tool (`search` ->
        # `search_catalog`) and one where they coincide (`get_cart`) — the
        # second only holds because role keys are namespaced.
        assert resolve_tool_annotation("search_catalog", None, scope) == "destructive"
        assert resolve_tool_annotation("get_cart", None, scope) == "destructive"
        assert resolve_step_label("get_cart", scope) == (
            "Getting the cart",
            "Got the cart",
        )

    def test_force_after_maps_through_the_binding(self, scope):
        # The pack names a ROLE KEY; the engine resolves it to this
        # template's tool so the forced render_ui think-step still fires.
        assert scope.tool_for(role_key("search")) == "find_products"
        assert scope.tool_for(role_key("update_cart")) == "update_cart"
        # A bare name is a literal tool, never re-read as a role — so the
        # plain string "search" cannot resolve to the catalog tool.
        assert scope.tool_for("search") == "search"
        assert scope.tool_for("not_a_role") == "not_a_role"


class TestRoleNamesAreNotToolNames:
    """A ROLE is a job the template binds to a tool; a tool name is a tool
    name. They share no key space, so a merchant's unrelated tool cannot
    inherit a role's metadata just by being named after it.

    ``search`` is the case that matters: it is commerce's catalog role,
    but its default tool is ``search_catalog`` — so a knowledge-base or
    order-lookup tool literally named ``search`` is a realistic collision
    on a commerce template, and it would pick up the catalog verifier,
    which rewrites a SUCCESSFUL result into an error envelope."""

    @pytest.fixture
    def scope(self):
        return resolve_flavor_scope(_template(), ["core", "commerce"])

    def test_a_tool_named_after_a_role_gets_nothing(self, scope):
        assert resolve_tool_annotation("search", None, scope) == "destructive"
        assert resolve_step_label("search", scope) == ("Searching", "Searched")

    def test_and_its_successful_result_is_not_rewritten(self, scope):
        knowledge_base_hit = {"status": "success", "answers": [{"a": "30 days"}]}
        assert run_tool_verifiers("search", {}, knowledge_base_hit, scope) is None

    def test_while_the_actual_bound_tool_still_resolves(self, scope):
        # The role is bound to search_catalog by default, so THAT keeps
        # every piece of its metadata.
        assert resolve_tool_annotation("search_catalog", None, scope) == "read_only"
        assert (
            run_tool_verifiers("search_catalog", {}, {"status": "success"}, scope)
            == "search result carries no products array"
        )


# ---------------------------------------------------------------------------
# Fail-open + degradation
# ---------------------------------------------------------------------------


def test_a_raising_role_map_costs_only_the_binding():
    from app.ai.voice.agents.breeze_buddy.chat import flavors

    def _boom(_template):
        raise RuntimeError("bad role map")

    flavors.register_flavor_roles("explosive", _boom)
    try:
        scope = resolve_flavor_scope(None, ["explosive", "commerce"])
        # The turn survives, commerce still resolves, and the broken
        # group's entries fall back to literal name matching.
        assert scope.groups == ("explosive", "commerce")
        assert resolve_tool_annotation("get_product", None, scope) == "read_only"
    finally:
        flavors._ROLE_MAPS.pop("explosive", None)


def test_no_groups_means_no_flavor_at_all():
    assert resolve_flavor_scope(_template(), None) is EMPTY_SCOPE
    assert resolve_flavor_scope(_template(), []) is EMPTY_SCOPE


# ---------------------------------------------------------------------------
# Catalog ownership
# ---------------------------------------------------------------------------


def test_two_groups_cannot_claim_the_same_primitive():
    # UI_CATALOG is a flat process-global map while the allowlist is
    # per-template, so a silent overwrite would hand one merchant's
    # component schema to another's session.
    with pytest.raises(ValueError, match="already registered by group"):
        register_primitives("impostor", {"ProductGrid": UI_CATALOG["ProductGrid"]})


def test_reregistering_within_the_same_group_is_still_fine():
    register_primitives("commerce", {"ProductGrid": UI_CATALOG["ProductGrid"]})
    assert group_for("ProductGrid") == "commerce"


def test_link_button_belongs_to_core():
    # The engine hardcodes LinkButton (render_ui component list, url trust
    # check, summary shape) and owns its config key, so the schema lives
    # in core — a flavor that wants different wording supplies it through
    # its render_ui pack instead.
    assert group_for("LinkButton") == "core"

"""chat/step_labels — label resolution (fallback + registered) and the
generic step-completion summarizer.

Flavor entries are group-scoped and role-keyed, so most assertions here
run against an explicit scope; the ones that DON'T are the point — they
pin that a session which never enabled the flavor is unaffected by it,
however warm the process is."""

from __future__ import annotations

from types import SimpleNamespace

from app.ai.voice.agents.breeze_buddy.chat.flavors import resolve_flavor_scope
from app.ai.voice.agents.breeze_buddy.chat.steps.labels import (
    register_step_labels,
    resolve_step_label,
    resolve_step_status,
    summarize_step_result,
)

_COMMERCE = resolve_flavor_scope(None, ["commerce"])

# ---------------------------------------------------------------------------
# resolve_step_label — generic humanizer fallback
# ---------------------------------------------------------------------------


def test_fallback_humanizes_verb_object_names():
    assert resolve_step_label("scan_shelf") == (
        "Scanning the shelf",
        "Scanned the shelf",
    )


def test_fallback_search_catalog_shape():
    # The canonical example from the UX spec. (The commerce flavor registers
    # the same strings explicitly; the fallback must match it unaided.)
    running, done = resolve_step_label("search_catalog")
    assert running == "Searching the catalog"
    assert done == "Searched the catalog"


def test_fallback_e_drop_and_multiword_object():
    assert resolve_step_label("update_shipping_address") == (
        "Updating the shipping address",
        "Updated the shipping address",
    )


def test_fallback_irregular_verb():
    assert resolve_step_label("get_order") == ("Getting the order", "Got the order")


def test_fallback_single_word_tool():
    # Single-word names still produce something human-ish, never a raw echo;
    # exact conjugation is best-effort.
    running, done = resolve_step_label("search")
    assert running == "Searching"
    assert done == "Searched"


def test_fallback_empty_name_is_safe():
    assert resolve_step_label("") == ("Working", "Done")


# ---------------------------------------------------------------------------
# resolve_step_label — registry
# ---------------------------------------------------------------------------


def test_registered_labels_win_over_humanizer():
    register_step_labels(
        "probe", {"frobnicate_widget": ("Frobnicating", "Frobnicated")}
    )
    scope = resolve_flavor_scope(None, ["probe"])
    assert resolve_step_label("frobnicate_widget", scope) == (
        "Frobnicating",
        "Frobnicated",
    )
    # ...but only for a session that enabled the group.
    assert resolve_step_label("frobnicate_widget") == (
        "Frobnicating the widget",
        "Frobnicated the widget",
    )


def test_commerce_registration_hook():
    # Importing the flavor schemas module (what ensure_group_loaded("commerce")
    # does) must register the commerce labels as a side effect.
    import app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas  # noqa: F401

    assert resolve_step_label("lookup_catalog", _COMMERCE) == (
        "Looking up products",
        "Looked up products",
    )
    assert resolve_step_label("update_cart", _COMMERCE) == (
        "Updating your cart",
        "Updated your cart",
    )
    assert resolve_step_label("get_cart", _COMMERCE) == (
        "Checking your cart",
        "Checked your cart",
    )


def test_commerce_labels_are_invisible_without_the_group():
    # The registry is process-global; the SCOPE is what makes it apply.
    # A merchant who never enabled commerce gets the humanizer for the
    # very same tool names.
    import app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas  # noqa: F401

    assert resolve_step_label("get_cart") == ("Getting the cart", "Got the cart")
    assert resolve_step_label("update_cart") == (
        "Updating the cart",
        "Updated the cart",
    )


def test_commerce_labels_follow_a_rebound_role():
    # A template whose gateway calls the search tool something else still
    # gets the commerce label — registration is keyed by ROLE.
    import app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas  # noqa: F401

    template = SimpleNamespace(
        configurations=SimpleNamespace(
            ui_intents=SimpleNamespace(tools={"search": "find_products"})
        )
    )
    scope = resolve_flavor_scope(template, ["commerce"])
    assert resolve_step_label("find_products", scope) == (
        "Searching the catalog",
        "Searched the catalog",
    )


# ---------------------------------------------------------------------------
# resolve_step_status
# ---------------------------------------------------------------------------


def test_status_error_envelope():
    assert resolve_step_status({"status": "error", "error": "boom"}) == "error"
    assert resolve_step_status({"status": "failed"}) == "error"


def test_status_ok_default():
    assert resolve_step_status({"products": []}) == "ok"
    assert resolve_step_status("plain string result") == "ok"
    assert resolve_step_status({"status": "success"}) == "ok"


# ---------------------------------------------------------------------------
# summarize_step_result — generic keys only
# ---------------------------------------------------------------------------


def test_summary_products_list():
    summary, count = summarize_step_result({"products": [{}, {}, {}]}, _COMMERCE)
    assert summary == "3 results"
    assert count == 3


def test_summary_products_singular():
    assert summarize_step_result({"products": [{}]}, _COMMERCE) == ("1 result", 1)


def test_summary_line_items():
    summary, count = summarize_step_result(
        {"id": "c1", "line_items": [{}, {}]}, _COMMERCE
    )
    assert summary == "cart updated · 2 items"
    assert count == 2


def test_summary_products_precedence_over_line_items():
    summary, count = summarize_step_result(
        {"products": [{}], "line_items": [{}, {}]}, _COMMERCE
    )
    assert (summary, count) == ("1 result", 1)


def test_summary_omitted_for_other_shapes():
    assert summarize_step_result({"status": "success"}, _COMMERCE) == (None, None)
    assert summarize_step_result("text", _COMMERCE) == (None, None)
    assert summarize_step_result(None, _COMMERCE) == (None, None)
    assert summarize_step_result({"products": "not-a-list"}, _COMMERCE) == (None, None)


def test_summary_omitted_entirely_without_the_group():
    # The shape-keyed summarizer is the likeliest of these registries to
    # fire on a stranger's payload — any tool returning a ``products``
    # list would have been captioned. Out of scope, it never runs.
    assert summarize_step_result({"products": [{}, {}]}) == (None, None)
    assert summarize_step_result({"line_items": [{}]}) == (None, None)

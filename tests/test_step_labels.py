"""chat/step_labels — label resolution (fallback + registered) and the
generic step-completion summarizer."""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.chat.steps.labels import (
    register_step_labels,
    resolve_step_label,
    resolve_step_status,
    summarize_step_result,
)

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
    register_step_labels({"frobnicate_widget": ("Frobnicating", "Frobnicated")})
    assert resolve_step_label("frobnicate_widget") == ("Frobnicating", "Frobnicated")


def test_commerce_registration_hook():
    # Importing the flavor schemas module (what ensure_group_loaded("commerce")
    # does) must register the commerce labels as a side effect.
    import app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas  # noqa: F401

    assert resolve_step_label("lookup_catalog") == (
        "Looking up products",
        "Looked up products",
    )
    assert resolve_step_label("update_cart") == (
        "Updating your cart",
        "Updated your cart",
    )
    assert resolve_step_label("get_cart") == (
        "Checking your cart",
        "Checked your cart",
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
    summary, count = summarize_step_result({"products": [{}, {}, {}]})
    assert summary == "3 results"
    assert count == 3


def test_summary_products_singular():
    assert summarize_step_result({"products": [{}]}) == ("1 result", 1)


def test_summary_line_items():
    summary, count = summarize_step_result({"id": "c1", "line_items": [{}, {}]})
    assert summary == "cart updated · 2 items"
    assert count == 2


def test_summary_products_precedence_over_line_items():
    summary, count = summarize_step_result({"products": [{}], "line_items": [{}, {}]})
    assert (summary, count) == ("1 result", 1)


def test_summary_omitted_for_other_shapes():
    assert summarize_step_result({"status": "success"}) == (None, None)
    assert summarize_step_result("text") == (None, None)
    assert summarize_step_result(None) == (None, None)
    assert summarize_step_result({"products": "not-a-list"}) == (None, None)

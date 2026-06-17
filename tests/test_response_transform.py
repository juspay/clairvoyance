# pyrefly: ignore-errors
# Test indexes into result dicts whose return type pyrefly can't infer
# tightly (`Cannot index into str/object`). Runtime correctness is covered.
"""Tests for in-place response transforms and the built-in scale_by_exponent.

Both pieces are channel-agnostic and vertical-agnostic: the walker doesn't
care who calls it, and the registered transform fn doesn't know about
voice vs chat — or about commerce, scheduling, or any other domain. These
tests exercise the walker shape-matching, the registry behaviour, and the
arithmetic rules for the first built-in op.
"""

from __future__ import annotations

# isort: off
# Order matters: template.types fully loads the `template` package (whose
# __init__ pulls in hooks/http_requester) BEFORE the utility import
# triggers handlers.transport.utils.__init__ → field_resolver →
# template.types again. Reversing these triggers a circular-import error.
from app.ai.voice.agents.breeze_buddy.template.types import ResponseTransform

from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.response_transform import (
    apply_response_transforms,
    derive_field,
    omit_fields,
    pick_fields,
    scale_by_exponent,
    strip_html,
)

# isort: on

# ---------------------------------------------------------------------------
# scale_by_exponent — pure arithmetic, no domain knowledge
# ---------------------------------------------------------------------------


def test_scale_by_exponent_default_two_integer_to_decimal_string():
    value = {"amount": 69995}
    scale_by_exponent(value, {})
    assert value["amount"] == "699.95"


def test_scale_by_exponent_default_two_digit_string():
    value = {"amount": "69995"}
    scale_by_exponent(value, {})
    assert value["amount"] == "699.95"


def test_scale_by_exponent_decimal_string_is_reformatted_consistently():
    # Already-decimal input is taken at face value (no re-scaling), but still
    # passes through the display formatter so output shape is consistent.
    value = {"amount": "885.95"}
    scale_by_exponent(value, {})
    assert value["amount"] == "885.95"


def test_scale_by_exponent_decimal_string_normalises_short_decimal():
    # "1585.9" comes back from upstream UCP — we want "1,585.90" for display
    # (thousands separator + exponent-aligned decimals).
    value = {"amount": "1585.9"}
    scale_by_exponent(value, {})
    assert value["amount"] == "1,585.90"


def test_scale_by_exponent_zero_keeps_integer_shape_with_separator():
    value = {"amount": 1000}
    scale_by_exponent(value, {"exponent": 0})
    assert value["amount"] == "1,000"


def test_scale_by_exponent_adds_thousands_separator_after_scaling():
    # Minor-unit input crossing the thousands boundary gets the separator.
    value = {"amount": 158590}
    scale_by_exponent(value, {})
    assert value["amount"] == "1,585.90"


def test_scale_by_exponent_three_emits_three_decimal_places():
    value = {"amount": 1234}
    scale_by_exponent(value, {"exponent": 3})
    assert value["amount"] == "1.234"


def test_scale_by_exponent_custom_amount_field():
    value = {"price": 1599}
    scale_by_exponent(value, {"amount_field": "price"})
    assert value["price"] == "15.99"


def test_scale_by_exponent_unknown_amount_field_is_noop():
    value = {"unrelated": "x"}
    scale_by_exponent(value, {"amount_field": "amount"})
    assert value == {"unrelated": "x"}


def test_scale_by_exponent_negative_exponent_is_noop():
    value = {"amount": 100}
    scale_by_exponent(value, {"exponent": -1})
    assert value == {"amount": 100}


def test_scale_by_exponent_non_numeric_amount_is_noop():
    value = {"amount": "not-a-number"}
    scale_by_exponent(value, {})
    assert value == {"amount": "not-a-number"}


def test_scale_by_exponent_non_dict_returns_unchanged():
    assert scale_by_exponent(42, {}) == 42
    assert scale_by_exponent("not a dict", {}) == "not a dict"
    assert scale_by_exponent(None, {}) is None


# ---------------------------------------------------------------------------
# apply_response_transforms — walker
# ---------------------------------------------------------------------------


def test_walker_mutates_nested_path():
    data = {"order": {"total": {"amount": 5000}}}
    apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="order.total", fn="scale_by_exponent", args={"exponent": 2}
            )
        ],
    )
    assert data["order"]["total"]["amount"] == "50.00"


def test_walker_iterates_arrays_with_wildcard():
    data = {
        "products": [
            {"id": "a", "price": {"amount": 69995}},
            {"id": "b", "price": {"amount": 12345}},
        ]
    }
    apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="products[*].price", fn="scale_by_exponent", args={"exponent": 2}
            )
        ],
    )
    assert data["products"][0]["price"]["amount"] == "699.95"
    assert data["products"][1]["price"]["amount"] == "123.45"
    # Other fields untouched.
    assert data["products"][0]["id"] == "a"


def test_walker_handles_deeply_nested_array_path():
    data = {
        "products": [
            {
                "price_range": {
                    "min": {"amount": 69995},
                    "max": {"amount": 99995},
                }
            }
        ]
    }
    apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="products[*].price_range.min",
                fn="scale_by_exponent",
                args={"exponent": 2},
            ),
            ResponseTransform(
                path="products[*].price_range.max",
                fn="scale_by_exponent",
                args={"exponent": 2},
            ),
        ],
    )
    assert data["products"][0]["price_range"]["min"]["amount"] == "699.95"
    assert data["products"][0]["price_range"]["max"]["amount"] == "999.95"


def test_walker_missing_key_is_silent_noop():
    data = {"order": {"id": "abc"}}
    apply_response_transforms(
        data,
        [ResponseTransform(path="order.total", fn="scale_by_exponent")],
    )
    assert data == {"order": {"id": "abc"}}


def test_walker_non_list_under_wildcard_is_silent_noop():
    data = {"products": {"not": "a list"}}
    apply_response_transforms(
        data,
        [ResponseTransform(path="products[*].price", fn="scale_by_exponent")],
    )
    assert data == {"products": {"not": "a list"}}


def test_walker_unknown_fn_is_logged_and_skipped():
    data = {"order": {"total": {"amount": 100}}}
    apply_response_transforms(
        data,
        [ResponseTransform(path="order.total", fn="nonexistent_fn")],
    )
    assert data["order"]["total"]["amount"] == 100


def test_walker_empty_transforms_returns_input_unchanged():
    data = {"a": 1}
    result = apply_response_transforms(data, [])
    assert result is data
    assert data == {"a": 1}


def test_walker_non_dict_root_returns_unchanged():
    result = apply_response_transforms(
        "just a string",
        [ResponseTransform(path="x", fn="scale_by_exponent")],
    )
    assert result == "just a string"


def test_walker_array_leaf_applies_fn_to_each_element():
    data = {
        "items": [
            {"amount": 100},
            {"amount": 250},
        ]
    }
    apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="items[*]", fn="scale_by_exponent", args={"exponent": 2}
            )
        ],
    )
    assert data["items"][0]["amount"] == "1.00"
    assert data["items"][1]["amount"] == "2.50"


def test_walker_multiple_transforms_applied_in_order():
    data = {"a": {"amount": 100}}
    apply_response_transforms(
        data,
        [
            ResponseTransform(path="a", fn="scale_by_exponent", args={"exponent": 2}),
            ResponseTransform(path="a", fn="scale_by_exponent", args={"exponent": 2}),
        ],
    )
    # Second pass is a no-op because amount is now a decimal string.
    assert data["a"]["amount"] == "1.00"


# ---------------------------------------------------------------------------
# pick_fields — projection, drops everything not in keep list
# ---------------------------------------------------------------------------


def test_pick_fields_keeps_only_listed_keys():
    value = {"id": "x", "title": "T", "variants": [1, 2], "collections": [3]}
    out = pick_fields(value, {"keep": ["id", "title"]})
    assert out == {"id": "x", "title": "T"}


def test_pick_fields_missing_keys_silently_dropped():
    out = pick_fields({"id": "x"}, {"keep": ["id", "title", "missing"]})
    assert out == {"id": "x"}


def test_pick_fields_empty_keep_returns_empty_dict():
    assert pick_fields({"a": 1, "b": 2}, {}) == {}
    assert pick_fields({"a": 1, "b": 2}, {"keep": []}) == {}


def test_pick_fields_non_dict_passes_through():
    assert pick_fields("nope", {"keep": ["x"]}) == "nope"
    assert pick_fields([1, 2], {"keep": ["x"]}) == [1, 2]


def test_pick_fields_via_walker_slims_each_array_element():
    data = {
        "products": [
            {"id": "a", "title": "A", "junk": "drop me", "variants": [1, 2]},
            {"id": "b", "title": "B", "junk": "drop me too", "variants": [3]},
        ]
    }
    apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="products[*]", fn="pick_fields", args={"keep": ["id", "title"]}
            )
        ],
    )
    assert data["products"] == [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]


# ---------------------------------------------------------------------------
# omit_fields — selective deletion, keeps everything not in drop list
# ---------------------------------------------------------------------------


def test_omit_fields_drops_listed_keys():
    out = omit_fields({"a": 1, "b": 2, "c": 3}, {"drop": ["a", "c"]})
    assert out == {"b": 2}


def test_omit_fields_missing_keys_silently_skipped():
    out = omit_fields({"a": 1}, {"drop": ["x", "y"]})
    assert out == {"a": 1}


def test_omit_fields_empty_drop_returns_input_unchanged():
    assert omit_fields({"a": 1}, {}) == {"a": 1}
    assert omit_fields({"a": 1}, {"drop": []}) == {"a": 1}


def test_omit_fields_via_walker_at_root_strips_envelope():
    # Most important real-world case: drop protocol metadata at the response
    # root so the LLM never sees the UCP envelope etc. Requires the caller
    # site to capture the return value (mcp/__init__.py does).
    data = {"ucp": {"v": 1}, "products": [{"id": "a"}], "messages": []}
    result = apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="", fn="omit_fields", args={"drop": ["ucp", "messages"]}
            )
        ],
    )
    assert result == {"products": [{"id": "a"}]}


# ---------------------------------------------------------------------------
# strip_html — strip tags, collapse whitespace, optional word-boundary clip
# ---------------------------------------------------------------------------


def test_strip_html_removes_tags_and_normalises_whitespace():
    assert strip_html("<p>Hello   <b>world</b>!</p>", {}) == "Hello world!"


def test_strip_html_truncates_at_word_boundary():
    s = "Vacuum-insulated bottle keeps drinks hot and cold for hours."
    out = strip_html(s, {"max_chars": 30})
    # 30-char window lands mid-word, so we clip at the prior space.
    assert out.endswith("…")
    assert len(out) <= 31  # 30 + ellipsis
    # The char right before the ellipsis must NOT be a space — confirms we
    # clipped at a word boundary (the space) rather than mid-word.
    assert out[-2] != " ", out
    # And the input MUST have been clipped (output is shorter than input).
    assert len(out) < len(s)


def test_strip_html_no_truncation_when_under_cap():
    assert strip_html("Short text.", {"max_chars": 100}) == "Short text."


def test_strip_html_zero_or_negative_max_chars_no_clip():
    s = "<p>Hello world</p>"
    assert strip_html(s, {"max_chars": 0}) == "Hello world"
    assert strip_html(s, {"max_chars": -5}) == "Hello world"


def test_strip_html_non_string_passes_through():
    assert strip_html({"html": "<p>x</p>"}, {}) == {"html": "<p>x</p>"}
    assert strip_html(None, {}) is None
    assert strip_html(42, {}) == 42


def test_strip_html_drops_tag_directly_before_punctuation():
    # A tag sitting immediately before closing punctuation must NOT leave a
    # space where it was (this is what the tag-site anchoring buys us).
    assert strip_html("<p>The bottle is <b>insulated</b>.</p>", {}) == (
        "The bottle is insulated."
    )
    assert strip_html("<p>done</p>.", {}) == "done."
    assert strip_html("Buy now<b>!</b>", {}) == "Buy now!"
    # Nested closes immediately before punctuation collapse cleanly too.
    assert strip_html("<p>It is <strong><em>great</em></strong>.</p>", {}) == (
        "It is great."
    )


def test_strip_html_preserves_spaces_around_punctuation_in_tag_free_text():
    # The old global "space before punctuation" pass corrupted tag-free text;
    # anchoring to tag sites leaves these untouched.
    assert strip_html("3 . 14", {}) == "3 . 14"
    assert strip_html("( a )", {}) == "( a )"
    assert strip_html("Mr . Smith", {}) == "Mr . Smith"
    assert strip_html("Wait ! Really ?", {}) == "Wait ! Really ?"
    assert strip_html("emoticon :) test", {}) == "emoticon :) test"


def test_strip_html_via_walker_on_nested_path():
    data = {
        "products": [
            {"description": {"html": "<p>The bottle is <b>vacuum-insulated</b>.</p>"}}
        ]
    }
    apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="products[*].description.html",
                fn="strip_html",
                args={"max_chars": 100},
            )
        ],
    )
    assert (
        data["products"][0]["description"]["html"] == "The bottle is vacuum-insulated."
    )


# ---------------------------------------------------------------------------
# Aggressive projection — the real-world search_catalog scenario
# ---------------------------------------------------------------------------


def test_combined_projection_strips_catalog_response_aggressively():
    """End-to-end: simulate the catalog-bloat fix on a UCP-shaped response."""
    data = {
        "ucp": {"version": "2026-04-08", "capabilities": {"x": "huge"}},
        "messages": [],
        "products": [
            {
                "id": "gid://shopify/Product/1",
                "title": "Test Bottle",
                "description": {"html": "<p>Reusable & <i>insulated</i>.</p>"},
                "price_range": {"min": {"amount": 51900, "currency": "INR"}},
                "media": [{"type": "image", "url": "https://x/y.jpg"}],
                "options": [{"name": "Color", "values": [{"label": "Red"}]}],
                "tags": ["best-seller"],
                # bloat we expect dropped:
                "variants": [{"id": "v1", "sku": "SKU1"} for _ in range(15)],
                "collections": [
                    {"id": "c1", "title": "Summer", "description": "x" * 1000}
                ],
                "url": "https://shop/product/1",
                "handle": "test-bottle",
                "gift_card": False,
            }
        ],
        "pagination": {"cursor": None, "has_more": False},
    }
    transforms = [
        ResponseTransform(
            path="", fn="omit_fields", args={"drop": ["ucp", "messages"]}
        ),
        ResponseTransform(
            path="products[*]",
            fn="pick_fields",
            args={
                "keep": [
                    "id",
                    "title",
                    "description",
                    "price_range",
                    "list_price_range",
                    "media",
                    "options",
                    "tags",
                ]
            },
        ),
        ResponseTransform(
            path="products[*].description.html",
            fn="strip_html",
            args={"max_chars": 200},
        ),
        ResponseTransform(
            path="products[*].price_range.min",
            fn="scale_by_exponent",
            args={"exponent": 2},
        ),
    ]
    result = apply_response_transforms(data, transforms)
    # Envelope gone.
    assert "ucp" not in result
    assert "messages" not in result
    # Pagination kept (we never asked to drop it).
    assert "pagination" in result
    # Product slimmed.
    p = result["products"][0]
    assert set(p.keys()) == {
        "id",
        "title",
        "description",
        "price_range",
        "media",
        "options",
        "tags",
    }
    # Variants & collections are the big spenders — confirm they're gone.
    assert "variants" not in p
    assert "collections" not in p
    assert "url" not in p
    # HTML stripped.
    assert p["description"]["html"] == "Reusable & insulated."
    # Scaling still works on the kept subtree.
    assert p["price_range"]["min"]["amount"] == "519.00"


# ---------------------------------------------------------------------------
# Regression: in-place mutation contract — justifies the deep-copy guard
# in http_handler.py around the apply_response_transforms call. If a
# transform raises mid-walk, the caller's payload would otherwise be
# left in a half-transformed state.
# ---------------------------------------------------------------------------


def test_walker_mutates_caller_payload_in_place():
    """apply_response_transforms MUTATES the input dict in place.

    This is the contract the http_handler deep-copy guard depends on:
    because mutation is in place, a mid-walk exception would otherwise
    surface a partially-transformed payload to the LLM. The handler
    therefore copies before calling and only swaps on success.
    """
    import copy as _copy

    data = {"order": {"total": {"amount": 5000}}}
    snapshot = _copy.deepcopy(data)

    apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="order.total", fn="scale_by_exponent", args={"exponent": 2}
            )
        ],
    )

    # Same dict identity — mutation was in place
    assert data["order"]["total"]["amount"] == "50.00"
    # The snapshot, taken from a deep-copy of the original, was untouched —
    # i.e. the http_handler's `transformed = copy.deepcopy(data)` pattern
    # produces a value the original input is decoupled from.
    assert snapshot["order"]["total"]["amount"] == 5000


# ---------------------------------------------------------------------------
# derive_field — regex-capture + format-template, no domain knowledge
# ---------------------------------------------------------------------------


def test_derive_field_positional_captures_format_into_template():
    value = {"id": "gid://shopify/Cart/AbCdEf?key=XyZ789"}
    derive_field(
        value,
        {
            "from": "id",
            "pattern": r"Cart/([^?]+)\?key=(.+)$",
            "template": "/cart/c/{0}?key={1}",
            "to": "claim_url",
        },
    )
    assert value["claim_url"] == "/cart/c/AbCdEf?key=XyZ789"
    # Source untouched.
    assert value["id"] == "gid://shopify/Cart/AbCdEf?key=XyZ789"


def test_derive_field_named_captures_format_into_template():
    value = {"id": "gid://shopify/Cart/AbCdEf?key=XyZ789"}
    derive_field(
        value,
        {
            "from": "id",
            "pattern": r"Cart/(?P<token>[^?]+)\?key=(?P<key>.+)$",
            "template": "/cart/c/{token}?key={key}",
            "to": "claim_url",
        },
    )
    assert value["claim_url"] == "/cart/c/AbCdEf?key=XyZ789"


def test_derive_field_via_walker_writes_sibling_field():
    data = {"cart": {"id": "gid://shopify/Cart/T?key=K", "checkout_url": "https://x"}}
    apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="cart",
                fn="derive_field",
                args={
                    "from": "id",
                    "pattern": r"Cart/([^?]+)\?key=(.+)$",
                    "template": "/cart/c/{0}?key={1}",
                    "to": "claim_url",
                },
            )
        ],
    )
    assert data["cart"]["claim_url"] == "/cart/c/T?key=K"
    # Adjacent fields preserved.
    assert data["cart"]["checkout_url"] == "https://x"


def test_derive_field_at_root_path_writes_root_field():
    # UCP cart shape: id lives at root, not under a `cart` wrapper.
    data = {"id": "gid://shopify/Cart/Tok123?key=abc", "line_items": []}
    apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="",
                fn="derive_field",
                args={
                    "from": "id",
                    "pattern": r"Cart/([^?]+)\?key=",
                    "template": "{0}",
                    "to": "cart_token",
                },
            )
        ],
    )
    assert data["cart_token"] == "Tok123"


def test_derive_field_no_match_is_noop():
    value = {"id": "not-a-shopify-cart"}
    derive_field(
        value,
        {
            "from": "id",
            "pattern": r"Cart/([^?]+)\?key=(.+)$",
            "template": "/cart/c/{0}?key={1}",
            "to": "claim_url",
        },
    )
    assert "claim_url" not in value


def test_derive_field_missing_source_is_noop():
    value = {"unrelated": "x"}
    derive_field(
        value,
        {
            "from": "id",
            "pattern": r".*",
            "template": "{0}",
            "to": "claim_url",
        },
    )
    assert value == {"unrelated": "x"}


def test_derive_field_non_string_source_is_noop():
    value = {"id": 123}
    derive_field(
        value,
        {
            "from": "id",
            "pattern": r".*",
            "template": "{0}",
            "to": "claim_url",
        },
    )
    assert "claim_url" not in value


def test_derive_field_missing_required_args_is_noop():
    value = {"id": "anything"}
    # No template
    derive_field(value, {"from": "id", "pattern": r".*", "to": "out"})
    assert "out" not in value
    # No pattern
    derive_field(value, {"from": "id", "template": "{0}", "to": "out"})
    assert "out" not in value
    # No `to`
    derive_field(value, {"from": "id", "pattern": r".*", "template": "{0}"})
    assert value == {"id": "anything"}


def test_derive_field_overwrite_false_skips_existing_destination():
    value = {"id": "abc-123", "out": "preexisting"}
    derive_field(
        value,
        {
            "from": "id",
            "pattern": r"(\w+)-(\d+)",
            "template": "{0}_{1}",
            "to": "out",
            "overwrite": False,
        },
    )
    assert value["out"] == "preexisting"


def test_derive_field_overwrite_true_replaces_existing():
    value = {"id": "abc-123", "out": "preexisting"}
    derive_field(
        value,
        {
            "from": "id",
            "pattern": r"(\w+)-(\d+)",
            "template": "{0}_{1}",
            "to": "out",
        },
    )
    assert value["out"] == "abc_123"


def test_derive_field_non_dict_returns_unchanged():
    assert derive_field("hello", {}) == "hello"
    assert derive_field(None, {}) is None
    assert derive_field([1, 2], {}) == [1, 2]


def test_derive_field_bad_regex_logs_and_is_noop():
    value = {"id": "x"}
    derive_field(
        value,
        {
            "from": "id",
            "pattern": r"(unclosed",
            "template": "{0}",
            "to": "out",
        },
    )
    assert "out" not in value


def test_derive_field_template_placeholder_mismatch_is_noop():
    value = {"id": "abc-123"}
    # Template references {5} but only 2 groups captured.
    derive_field(
        value,
        {
            "from": "id",
            "pattern": r"(\w+)-(\d+)",
            "template": "{5}",
            "to": "out",
        },
    )
    assert "out" not in value


def test_derive_field_malformed_template_is_noop():
    """Malformed format strings (bare ``{``, unbalanced braces, …) raise
    ValueError from ``str.format`` — must be caught and no-op'd per the
    documented contract, not propagate and abort the whole pass.
    """
    value = {"id": "abc-123"}
    derive_field(
        value,
        {
            "from": "id",
            "pattern": r"(\w+)-(\d+)",
            "template": "{",
            "to": "out",
        },
    )
    assert "out" not in value
    # And as part of a larger transform pass — must not raise upstream.
    data = {"products": [{"id": "ok-1"}, {"id": "ok-2"}]}
    apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="products[*]",
                fn="derive_field",
                args={
                    "from": "id",
                    "pattern": r"(\w+)-(\d+)",
                    "template": "{",
                    "to": "out",
                },
            )
        ],
    )
    # Original values preserved, no "out" field added.
    assert data["products"] == [{"id": "ok-1"}, {"id": "ok-2"}]

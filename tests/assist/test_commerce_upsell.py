"""Unit pins for the cart-upsell deterministic curation pass
(assist/commerce/upsell.py): size-continuity filtering + feature-variant
stamping + already-in-cart exclusion. The LLM picker and the search
round-trip are exercised live (e2e wire check), not here."""

from app.ai.voice.agents.breeze_buddy.assist.commerce.upsell import (
    build_upsell_selectors,
    match_size_variant,
)


def _product(pid, title, sizes_available=None, options=None):
    """Candidate with a Size axis; ``sizes_available`` maps label → bool."""
    sizes_available = sizes_available or {}
    return {
        "id": pid,
        "title": title,
        "options": (
            options
            if options is not None
            else [{"name": "Size", "values": [{"label": s} for s in sizes_available]}]
        ),
        "variants": [
            {
                "id": f"{pid}-{label}",
                "title": label,
                "options": [{"name": "Size", "label": label}],
                "availability": {"available": avail},
            }
            for label, avail in sizes_available.items()
        ],
    }


ADDED_TOKENS = {"effortless", "leggings", "black", "xl"}


class TestMatchSizeVariant:
    def test_available_size_match_returns_variant_id(self):
        p = _product("p1", "Zipper Vest", {"M": True, "XL": True})
        assert match_size_variant(p, ADDED_TOKENS) == "p1-XL"

    def test_matching_size_sold_out_drops(self):
        from app.ai.voice.agents.breeze_buddy.assist.commerce import upsell

        p = _product("p1", "Zipper Vest", {"M": True, "XL": False})
        assert match_size_variant(p, ADDED_TOKENS) is upsell._DROP

    def test_no_size_axis_keeps_unstamped(self):
        p = _product("p1", "Sipper Bottle", options=[])
        assert match_size_variant(p, ADDED_TOKENS) is None

    def test_disjoint_size_vocabulary_keeps_unstamped(self):
        # Sock sizing ("Free Size") shares no label with the added apparel
        # size — keep the product, let the picker handle the choice.
        p = _product("p1", "Crew Socks", {"Free Size": True})
        assert match_size_variant(p, ADDED_TOKENS) is None


class TestBuildUpsellSelectors:
    def test_stamps_filters_and_excludes_cart(self):
        products = [
            _product("keep", "Zipper Vest", {"XL": True}),
            _product("soldout", "Mesh Top", {"XL": False}),
            _product("incart", "Effortless Leggings - Black", {"XL": True}),
            _product("nosize", "Sipper Bottle", options=[]),
        ]
        selectors = build_upsell_selectors(
            products,
            added_tokens=ADDED_TOKENS,
            cart_titles=["Effortless Leggings - Black - XL"],
        )
        assert selectors == [
            {"id": "keep", "feature_variant": "keep-XL"},
            {"id": "nosize"},
        ]

    def test_caps_at_max_items(self):
        products = [_product(f"p{i}", f"Vest {i}", {"XL": True}) for i in range(10)]
        selectors = build_upsell_selectors(
            products, added_tokens=ADDED_TOKENS, cart_titles=[], max_items=3
        )
        assert len(selectors) == 3

    def test_malformed_entries_skipped(self):
        selectors = build_upsell_selectors(
            [None, {"title": "no id"}, 42],
            added_tokens=ADDED_TOKENS,
            cart_titles=[],
        )
        assert selectors == []

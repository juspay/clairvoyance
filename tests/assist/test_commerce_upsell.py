"""Unit pins for the cart-upsell deterministic curation pass
(assist/commerce/ucp/upsell.py): size-continuity filtering + feature-variant
stamping + already-in-cart exclusion. The LLM picker and the search
round-trip are exercised live (e2e wire check), not here."""

import pytest

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.upsell import (
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
        from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp import upsell

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


class TestOptInGate:
    """The upsell is merchandising, so it is OFF unless the template asks
    for it (``flavor.ucp.features.upsell``). Enabling the commerce flavor
    must not silently start spending an LLM call + catalog search on every
    add_to_cart, nor put recommendations a merchant didn't choose into
    their thread.
    """

    @staticmethod
    def _agent(features):
        from types import SimpleNamespace

        return SimpleNamespace(
            template=SimpleNamespace(
                configurations=SimpleNamespace(
                    ui_intents=None,
                    flavor={"ucp": SimpleNamespace(connectors=[], features=features)},
                )
            )
        )

    @pytest.mark.asyncio
    async def test_off_by_default_and_does_no_work(self, monkeypatch):
        from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp import upsell

        async def _must_not_run(*_a, **_k):
            raise AssertionError("_run must not be reached when opted out")

        monkeypatch.setattr(upsell, "_run", _must_not_run)
        # No features block at all, and an explicit false — both stay off.
        for features in ({}, {"upsell": False}):
            assert (
                await upsell.run_cart_upsell(
                    self._agent(features), None, {}, None, "t1", "update_cart", {}
                )
                is None
            )

    @pytest.mark.asyncio
    async def test_opted_in_template_runs_it(self, monkeypatch):
        from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp import upsell

        async def _fake_run(*_a, **_k):
            return {"op": "add", "type": "ProductGrid"}

        monkeypatch.setattr(upsell, "_run", _fake_run)
        assert await upsell.run_cart_upsell(
            self._agent({"upsell": True}), None, {}, None, "t1", "update_cart", {}
        ) == {"op": "add", "type": "ProductGrid"}

    @pytest.mark.asyncio
    async def test_missing_template_stays_off(self, monkeypatch):
        """A caller without a resolvable template must not accidentally
        opt in — fail closed, not open."""
        from types import SimpleNamespace

        from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp import upsell

        async def _must_not_run(*_a, **_k):
            raise AssertionError("_run must not be reached without a template")

        monkeypatch.setattr(upsell, "_run", _must_not_run)
        assert (
            await upsell.run_cart_upsell(
                SimpleNamespace(template=None), None, {}, None, "t1", "update_cart", {}
            )
            is None
        )

"""The UCP ↔ connector seam contract.

UCP is the protocol; a connector is a platform that serves it. The seam
has to hold three properties or the split is cosmetic:

1. **Empty chain = pure UCP.** With no connector registered the
   projections still work — a gateway that ships clean data pays nothing.
2. **First opinion wins**, and "no opinion" (None) falls through to the
   next connector, then to UCP's own behavior.
3. **A connector is never load-bearing** — one that raises is skipped, not
   propagated. A broken platform hook must not fail a shopper's request.
"""

from __future__ import annotations

import pytest

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp import hooks


@pytest.fixture
def empty_chains(monkeypatch):
    """Run against a pristine seam — the real Shopify connector registers
    process-globally as soon as any sibling test imports the flavor."""
    monkeypatch.setattr(hooks, "_MEDIA_RESOLVERS", [])
    monkeypatch.setattr(hooks, "_VARIANT_NORMALIZERS", [])
    monkeypatch.setattr(hooks, "_DESCRIPTION_REPAIRS", [])


class TestEmptyChainIsPureUcp:
    @pytest.mark.asyncio
    async def test_no_media_resolver_means_no_gallery(self, empty_chains):
        assert await hooks.resolve_media(None, {"url": "https://x/products/y"}) is None

    def test_no_normalizer_projects_variants_verbatim(self, empty_chains):
        variants = [{"id": "v1", "title": "Default Title"}]
        assert hooks.normalize_variants(variants) == variants

    def test_no_repair_returns_text_unchanged(self, empty_chains):
        assert hooks.repair_description("plain text") == "plain text"


class TestFirstOpinionWins:
    def test_none_falls_through_to_the_next_connector(self, empty_chains):
        hooks.register_variant_normalizer(lambda _v: None)
        hooks.register_variant_normalizer(lambda _v: [{"id": "second"}])
        assert hooks.normalize_variants([{"id": "orig"}]) == [{"id": "second"}]

    def test_repairs_compose_in_order(self, empty_chains):
        hooks.register_description_repair(lambda t: t + " one")
        hooks.register_description_repair(lambda t: t + " two")
        assert hooks.repair_description("start") == "start one two"


class TestConnectorIsNeverLoadBearing:
    @pytest.mark.asyncio
    async def test_raising_media_resolver_is_skipped(self, empty_chains):
        def _boom(_session, _product):
            raise RuntimeError("connector is broken")

        hooks.register_media_resolver("brokenconnector", _boom)
        # Does not raise — falls through to "no gallery".
        assert await hooks.resolve_media(None, {"url": "u"}) is None

    def test_raising_normalizer_falls_back_to_ucp(self, empty_chains):
        def _boom(_v):
            raise RuntimeError("connector is broken")

        hooks.register_variant_normalizer(_boom)
        variants = [{"id": "v1"}]
        assert hooks.normalize_variants(variants) == variants

    def test_raising_repair_leaves_text_intact(self, empty_chains):
        def _boom(_t):
            raise RuntimeError("connector is broken")

        hooks.register_description_repair(_boom)
        assert hooks.repair_description("text") == "text"


class TestConnectorAllowlist:
    """``flavor.<protocol>.connectors`` narrows the media chain. Empty or
    unset keeps the zero-config default: every resolver self-selects."""

    @staticmethod
    async def _always(_session, _product):
        return [{"type": "image", "url": "a"}, {"type": "image", "url": "b"}]

    @pytest.mark.asyncio
    async def test_unset_allowlist_consults_everyone(self, empty_chains):
        hooks.register_media_resolver("someplatform", self._always)
        assert await hooks.resolve_media(None, {}) is not None
        # An explicitly empty list means the same thing as unset.
        assert await hooks.resolve_media(None, {}, allowed=[]) is not None

    @pytest.mark.asyncio
    async def test_named_allowlist_skips_everyone_else(self, empty_chains):
        hooks.register_media_resolver("someplatform", self._always)
        assert await hooks.resolve_media(None, {}, allowed=["other"]) is None
        assert await hooks.resolve_media(None, {}, allowed=["someplatform"]) is not None


class TestBothProjectionsHonourTheNormalizer:
    """ProductP (card) and ProductDetailP (overlay) call the SAME hook, so
    a filtering normalizer must produce the same variant set in both — a
    card whose picker disagrees with the overlay's is a bug the shopper
    sees."""

    def test_filtering_normalizer_applies_to_card_and_detail_alike(self, empty_chains):
        from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas import (
            ProductDetailP,
            ProductP,
        )

        hooks.register_variant_normalizer(lambda vs: [vs[0]] if vs else None)
        raw = {
            "id": "p1",
            "title": "Tee",
            "price_range": {"min": {"amount": 4999.0, "currency": "INR"}},
            "variants": [
                {"id": "v1", "title": "S", "availability": "available"},
                {"id": "v2", "title": "M", "availability": "available"},
            ],
        }
        card = [v.id for v in ProductP.model_validate(dict(raw)).variants]
        detail = [v.id for v in ProductDetailP.model_validate(dict(raw)).variants]
        assert card == detail == ["v1"]


class TestEmptyGalleryEndsTheChain:
    @pytest.mark.asyncio
    async def test_empty_list_is_an_opinion_not_a_pass(self, empty_chains):
        """`[]` means "this platform has no gallery, stop asking" — the
        same contract the other two chains use."""

        async def _no_gallery(_s, _p):
            return []

        async def _would_run(_s, _p):
            raise AssertionError("chain must stop at the first opinion")

        hooks.register_media_resolver("first", _no_gallery)
        hooks.register_media_resolver("second", _would_run)
        assert await hooks.resolve_media(None, {}) == []


def test_flavor_block_tolerates_an_explicit_null():
    """Every sibling config block (ui_intents, ui_catalog, render_ui) is
    null-tolerant; `"flavor": null` must not fail the whole template
    decode and take the merchant's agent down with it."""
    from app.ai.voice.agents.breeze_buddy.template.types import ConfigurationModel

    assert ConfigurationModel.model_validate({"flavor": None}).flavor is None


def test_registration_is_idempotent(empty_chains):
    def _fn(_v):
        return None

    hooks.register_variant_normalizer(_fn)
    hooks.register_variant_normalizer(_fn)
    assert hooks._VARIANT_NORMALIZERS.count(_fn) == 1

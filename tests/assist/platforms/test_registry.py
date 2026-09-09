from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    Signal,
    SiteProfile,
    StoreResearch,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms import registry


def _profile(**inline):
    return SiteProfile(
        url="https://hustleculture.co.in",
        final_url="https://hustleculture.co.in/",
        status=200,
        inline_literals=inline,
    )


def test_registry_resolves_known_adapters_and_refuses_unknown():
    assert registry.resolve("shopify").id == "shopify"
    assert registry.resolve("generic").id == "generic"
    try:
        registry.resolve("magento")
    except KeyError:
        pass
    else:
        raise AssertionError("unknown adapter resolved")


def test_hustleculture_signals_classify_as_shopify():
    signals = [
        Signal(kind="script_src", pattern="https://cdn.shopify.com/s/files/x.js"),
        Signal(kind="cookie_key", pattern="_shopify_y"),
        Signal(kind="js_literal", pattern="Shopify.theme"),
    ]
    adapter, confidence = registry.classify(signals)
    assert adapter.id == "shopify" and confidence >= 1.0


def test_no_signals_falls_back_to_generic():
    adapter, confidence = registry.classify([])
    assert adapter.id == "generic" and confidence == 0.0


def test_shopify_identity_uses_the_permanent_domain():
    identity = registry.resolve("shopify").identity(
        _profile(**{"Shopify.shop": "9b1086-18.myshopify.com"})
    )
    assert identity.canonical_host == "hustleculture.co.in"
    assert identity.permanent_host == "9b1086-18.myshopify.com"
    origins = registry.resolve("shopify").extra_origins(
        identity,
        StoreResearch(
            platform="shopify", canonical_origin="https://hustleculture.co.in"
        ),
    )
    assert origins == ["https://9b1086-18.myshopify.com"]


def test_generic_adapter_is_the_zero_adapter_path():
    generic = registry.resolve("generic")
    identity = generic.identity(_profile())
    research = StoreResearch(
        platform="generic", canonical_origin="https://hustleculture.co.in"
    )
    assert (
        generic.operating_sections() == [] and generic.tools(identity, research) == []
    )
    assert (
        generic.install() == "snippet"
        and generic.mirror_policy().cart_handoff == "link"
    )
    assert registry.resolve("shopify").install() == "theme_embed"
    assert registry.resolve("shopify").mirror_policy().cart_handoff == "permalink"

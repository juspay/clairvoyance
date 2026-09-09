from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    Signal,
    SiteProfile,
    SiteResearch,
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
    # The shapes the probe actually emits: a host for a script source, a
    # cookie name, an assignment name.
    signals = [
        Signal(kind="script_src", pattern="cdn.shopify.com"),
        Signal(kind="cookie_key", pattern="_shopify_y"),
        Signal(kind="js_literal", pattern="Shopify.theme"),
    ]
    adapter, confidence = registry.classify(signals)
    assert adapter.id == "shopify" and confidence >= 1.0


def test_a_lookalike_host_does_not_classify_as_shopify():
    # A site loading a script from a host that merely starts with the real
    # CDN must not be able to pass itself off as that platform.
    signals = [
        Signal(kind="script_src", pattern="cdn.shopify.com.attacker.net"),
        Signal(kind="header", pattern="x-custom: powered-by: shopify"),
    ]
    adapter, confidence = registry.classify(signals)
    assert adapter.id == "generic" and confidence == 0.0


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
        SiteResearch(
            platform="shopify", canonical_origin="https://hustleculture.co.in"
        ),
    )
    assert origins == ["https://9b1086-18.myshopify.com"]


def test_generic_adapter_is_the_zero_adapter_path():
    generic = registry.resolve("generic")
    identity = generic.identity(_profile())
    research = SiteResearch(
        platform="generic", canonical_origin="https://hustleculture.co.in"
    )
    assert generic.tools(identity, research) == []
    assert generic.install() == "snippet" and generic.mirror_policy().handoff == "link"
    assert registry.resolve("shopify").install() == "theme_embed"
    assert registry.resolve("shopify").mirror_policy().handoff == "permalink"


def test_host_apps_and_request_platform_resolve_through_the_registry():
    for host_app in ("breeze-buddy", "buddy-assist"):
        adapter = registry.for_host_app(host_app)
        assert adapter.id == "shopify"
        reseller, merchant = adapter.tenant(host_app, "acme.myshopify.com")
        assert reseller in ("BB_SHOPIFY", "BB_ASSIST") and merchant.endswith(
            "acme.myshopify.com"
        )
    try:
        registry.for_host_app("some-other-app")
    except KeyError:
        pass
    else:
        raise AssertionError("unknown host app resolved")
    assert registry.for_request("shopify").id == "shopify"
    assert registry.for_request("web").id == "generic"
    assert registry.for_request(None).id == "generic"


def test_foreign_ownership_is_symmetric():
    shopify, generic = registry.resolve("shopify"), registry.resolve("generic")
    assert "shopify-storefront-ucp" in registry.foreign_mcp_server_names(generic)
    assert registry.foreign_mcp_server_names(shopify) == frozenset()
    assert "state_reducers" in registry.foreign_tool_config_keys(generic)
    assert registry.foreign_tool_config_keys(shopify) == ()
    assert registry.foreign_payload_keys(generic) == ("shopify_customer_token",)
    assert "{{#shopify_operating_section}}" in registry.legacy_section_markers()
    assert shopify.store_name("acme.myshopify.com") == "acme"
    assert generic.store_name("acme.example.com") == "acme.example.com"

"""Stage 2: the right adapter, for stated reasons, and the generic fallback.

The last two tests are the engine's founding rule in executable form — an
unrecognised site is handled, not rejected, and a site we have no adapter for
is never dressed up as one we do.
"""

from __future__ import annotations

import pathlib

from app.ai.voice.agents.breeze_buddy.assist.engine.classify import classify_profile
from app.ai.voice.agents.breeze_buddy.assist.engine.probe import profile_from_page
from app.ai.voice.agents.breeze_buddy.assist.platforms import registry

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "probe"

HOSTED_HEADERS = {"powered-by": "Shopify", "content-type": "text/html; charset=utf-8"}
HOSTED_COOKIES = ["_shopify_y", "_shopify_s"]


def _profile(name: str, *, url: str, headers=None, cookies=(), status: int = 200):
    return profile_from_page(
        url=url,
        final_url=url,
        status=status,
        headers=headers or {"content-type": "text/html; charset=utf-8"},
        cookie_names=list(cookies),
        body=(FIXTURES / name).read_text(),
    )


def test_a_hosted_storefront_is_recognised_with_its_permanent_host() -> None:
    profile = _profile(
        "shopify_home.html",
        url="https://hustleculture.co.in/",
        headers=HOSTED_HEADERS,
        cookies=HOSTED_COOKIES,
    )
    result = classify_profile(profile)

    assert result.adapter_id == "shopify"
    assert result.confidence >= registry.CLASSIFY_THRESHOLD
    # The custom domain a shopper types, and the permanent one the tenant is
    # keyed by — both, from one page.
    assert result.identity.canonical_host == "hustleculture.co.in"
    assert result.identity.permanent_host == "9b1086-18.myshopify.com"
    assert result.scores["generic"] == 0.0


def test_the_verdict_says_what_convinced_it() -> None:
    profile = _profile(
        "shopify_home.html",
        url="https://hustleculture.co.in/",
        headers=HOSTED_HEADERS,
        cookies=HOSTED_COOKIES,
    )
    result = classify_profile(profile)
    matched = {(signal.kind, signal.pattern) for signal in result.matched}

    assert ("cookie_key", "_shopify_y") in matched
    assert ("js_literal", "Shopify.theme") in matched
    assert ("script_src", "cdn.shopify.com") in matched
    # Only signals that carried weight are reported, not the whole page.
    assert len(result.matched) < len(profile.signals)


def test_confidence_is_reported_on_a_zero_to_one_scale() -> None:
    # A page carrying every marker an adapter knows scores past its own
    # ceiling; a number over 1.0 shown as "confidence" would be nonsense.
    profile = _profile(
        "shopify_home.html",
        url="https://hustleculture.co.in/",
        headers=HOSTED_HEADERS,
        cookies=HOSTED_COOKIES,
    )
    result = classify_profile(profile)
    assert result.confidence == 1.0
    assert all(0.0 <= score <= 1.0 for score in result.scores.values())


def test_a_site_on_no_platform_falls_back_to_generic() -> None:
    profile = _profile("plain_home.html", url="https://nilgiri-roasters.example/")
    result = classify_profile(profile)

    assert result.adapter_id == "generic"
    assert result.confidence == 0.0
    assert result.identity.canonical_host == "nilgiri-roasters.example"
    assert result.identity.permanent_host is None
    assert result.matched == []


def test_a_platform_we_have_no_adapter_for_is_generic_not_a_wrong_guess() -> None:
    # A WordPress site is a real platform with no adapter yet. The honest
    # answer is the generic path, never the nearest adapter we happen to own.
    profile = _profile(
        "woocommerce_home.html",
        url="https://woocommerce.com/",
        headers={"x-powered-by": "WordPress VIP", "content-type": "text/html"},
    )
    result = classify_profile(profile)

    assert result.adapter_id == "generic"
    assert result.scores["shopify"] < registry.CLASSIFY_THRESHOLD
    assert result.identity.canonical_host == "woocommerce.com"


def test_an_interstitial_does_not_become_a_confident_verdict() -> None:
    profile = profile_from_page(
        url="https://guarded.example/",
        final_url="https://guarded.example/",
        status=403,
        headers={"cf-mitigated": "challenge", "server": "cloudflare"},
        cookie_names=[],
        body="<html><head><title>Just a moment...</title></head></html>",
    )
    result = classify_profile(profile)

    assert profile.challenge is True
    assert result.adapter_id == "generic"


def test_a_page_cannot_forge_its_way_into_an_adapter() -> None:
    # Everything here is attacker-chosen: a script host that merely starts
    # with the real CDN, a header value planted under a header of its own
    # naming, and a claimed permanent domain. None of it should count.
    profile = profile_from_page(
        url="https://attacker.example/",
        final_url="https://attacker.example/",
        status=200,
        headers={"x-custom": "powered-by: shopify"},
        cookie_names=["_shopify_y_not_really"],
        body="""
        <html><head>
          <script src="https://cdn.shopify.com.attacker.net/a.js"></script>
          <script>Shopify = {}; Shopify.shop = "victim-store.example";</script>
        </head></html>
        """,
    )
    result = classify_profile(profile)

    assert result.adapter_id == "generic"
    assert result.identity.permanent_host is None


def test_a_claimed_permanent_domain_is_only_taken_in_the_right_shape() -> None:
    # The value comes out of a page anyone can write and goes on to name a
    # tenant, so anything that is not the minted shape is dropped entirely.
    from app.ai.voice.agents.breeze_buddy.assist.platforms.shopify.adapter import (
        _permanent_host,
    )

    assert _permanent_host("9b1086-18.myshopify.com") == "9b1086-18.myshopify.com"
    assert _permanent_host("  9B1086-18.MyShopify.com ") == "9b1086-18.myshopify.com"
    for claimed in [
        None,
        "",
        "victim.example",
        "evil.example/9b1086-18.myshopify.com",
        "a.b.myshopify.com",
        ".myshopify.com",
        "-bad.myshopify.com",
        "9b1086-18.myshopify.com.attacker.net",
    ]:
        assert _permanent_host(claimed) is None, claimed

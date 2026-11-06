"""Stage 1 against captured pages: what the probe writes down, and what it does not.

The fixtures are trimmed heads of pages fetched on 2026-09-08 — a hosted
storefront, a WordPress site, and a hand-written static site on no platform at
all. Replaying them keeps the parser honest against real markup without a
socket.
"""

from __future__ import annotations

import json
import pathlib

from app.ai.voice.agents.breeze_buddy.assist.engine.probe import (
    is_challenge,
    profile_from_page,
    summarize,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.html_facts import PageFacts

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "probe"

# From the same capture as the body fixture.
HOSTED_HEADERS = {
    "content-type": "text/html; charset=utf-8",
    "powered-by": "Shopify",
    "x-frame-options": "DENY",
    "server": "cloudflare",
}
HOSTED_COOKIES = ["_shopify_y", "_shopify_s", "localization", "_shopify_essential"]


def _profile(name: str, *, url: str, headers=None, cookies=(), status: int = 200):
    return profile_from_page(
        url=url,
        final_url=url,
        status=status,
        headers=headers or {"content-type": "text/html; charset=utf-8"},
        cookie_names=list(cookies),
        body=(FIXTURES / name).read_text(),
    )


def test_a_hosted_storefront_yields_its_markers() -> None:
    profile = _profile(
        "shopify_home.html",
        url="https://hustleculture.co.in/",
        headers=HOSTED_HEADERS,
        cookies=HOSTED_COOKIES,
    )
    kinds = {signal.kind for signal in profile.signals}
    assert {
        "script_src",
        "cookie_key",
        "header",
        "meta",
        "js_literal",
        "js_global",
    } <= kinds
    # The value the platform's adapter needs to know which tenant this is.
    assert profile.inline_literals["Shopify.shop"] == "9b1086-18.myshopify.com"
    assert profile.script_hosts.get("cdn.shopify.com")
    assert profile.meta["og:site_name"] == "Hustle Culture"
    assert profile.challenge is False


def test_a_site_on_no_platform_still_yields_a_usable_profile() -> None:
    # The generic path is the one that has to work everywhere, so a plain
    # site must come back with everything except the platform markers.
    profile = _profile("plain_home.html", url="https://nilgiri-roasters.example/")
    assert profile.title == "Nilgiri Coffee Roasters"
    assert profile.meta["description"].startswith("Single-estate coffee")
    assert profile.inline_literals == {}
    assert [signal for signal in profile.signals if signal.kind == "js_literal"] == []
    assert profile.script_hosts == {
        "nilgiri-roasters.example": 1,
        "www.googletagmanager.com": 1,
    }
    assert {block["@type"] for block in profile.json_ld} == {"Organization"}


def test_relative_and_absolute_script_sources_both_resolve() -> None:
    facts = PageFacts(
        "https://shop.example/",
        """
        <html><head>
          <script src="/assets/a.js"></script>
          <script src="//cdn.example.net/b.js"></script>
          <script src="https://cdn.example.net/c.js"></script>
        </head></html>
        """,
    )
    assert facts.script_hosts() == {"shop.example": 1, "cdn.example.net": 2}


def test_broken_markup_keeps_what_was_parsed() -> None:
    # Storefront markup is routinely invalid; a probe that threw would fail
    # on exactly the sites that need onboarding most.
    profile = profile_from_page(
        url="https://messy.example/",
        final_url="https://messy.example/",
        status=200,
        headers={},
        cookie_names=[],
        body='<html><head><meta name="description" content="ok"><title>Messy'
        "<div><p>unclosed everywhere",
    )
    assert profile.meta["description"] == "ok"
    assert profile.status == 200


def test_cookie_values_never_enter_the_profile() -> None:
    # The probe records that a cookie was set, never what was in it: the
    # value is the site's session material.
    profile = _profile(
        "shopify_home.html",
        url="https://hustleculture.co.in/",
        headers=HOSTED_HEADERS,
        cookies=HOSTED_COOKIES,
    )
    assert profile.cookies == HOSTED_COOKIES
    assert "set-cookie" not in profile.headers
    assert not any(
        "=" in signal.pattern
        for signal in profile.signals
        if signal.kind == "cookie_key"
    )


def test_an_interstitial_is_recorded_as_one_not_read_as_the_site() -> None:
    assert is_challenge(403, {"cf-mitigated": "challenge"}, "")
    assert is_challenge(200, {}, "<title>Just a moment...</title>")
    # A short refusal with no marker is still an interstitial for our purpose.
    assert is_challenge(429, {}, "too many requests")
    # A real page that answers 403 for its own reasons is not.
    assert not is_challenge(403, {}, "<html>" + "x" * 8000 + "</html>")
    assert not is_challenge(200, {}, "<html>a normal page</html>")


def test_the_report_summary_is_the_operator_facing_half() -> None:
    profile = _profile(
        "shopify_home.html",
        url="https://hustleculture.co.in/",
        headers=HOSTED_HEADERS,
        cookies=HOSTED_COOKIES,
    )
    summary = summarize(profile)
    assert summary["name"] == "Hustle Culture"
    assert summary["challenge"] is False
    assert summary["size_bytes"] > 0 and summary["fetched_at"] is not None

    # Script hosts carry their counts, busiest first: the topology is what
    # says how hard a site will be to mirror, not the list of names.
    hosts = summary["script_hosts"]
    assert any(entry["host"] == "cdn.shopify.com" for entry in hosts)
    assert all(entry["scripts"] >= 1 for entry in hosts)
    assert [entry["scripts"] for entry in hosts] == sorted(
        (entry["scripts"] for entry in hosts), reverse=True
    )

    # Types are complete; the blocks themselves ride along so the research
    # stage need not re-fetch a page already parsed here.
    assert any(kind == "BreadcrumbList" for kind in summary["structured_data_types"])
    assert summary["structured_data"]
    assert all(isinstance(block, dict) for block in summary["structured_data"])


def test_oversized_structured_data_is_left_out_but_still_typed() -> None:
    # The blocks are third-party content, so the payload is bounded. Dropping
    # one must not lose the fact that its type was present.
    big = {"@context": "https://schema.org", "@type": "Organization", "x": "y" * 9000}
    small = {"@context": "https://schema.org", "@type": "WebSite", "name": "Small"}
    body = (
        "<html><head>"
        f'<script type="application/ld+json">{json.dumps(big)}</script>'
        f'<script type="application/ld+json">{json.dumps(small)}</script>'
        "</head></html>"
    )
    profile = profile_from_page(
        url="https://verbose.example/",
        final_url="https://verbose.example/",
        status=200,
        headers={},
        cookie_names=[],
        body=body,
    )
    summary = summarize(profile)
    assert summary["structured_data_types"] == ["Organization", "WebSite"]
    assert [block["@type"] for block in summary["structured_data"]] == ["WebSite"]


def test_header_values_are_kept_printable() -> None:
    # Header values are written by the site being looked at and travel into
    # the report and anything that stores it.
    profile = profile_from_page(
        url="https://noisy.example/",
        final_url="https://noisy.example/",
        status=200,
        headers={"x-weird": "ok\x00\x07value", "server": "nginx"},
        cookie_names=[],
        body="<html></html>",
    )
    patterns = {s.pattern for s in profile.signals if s.kind == "header"}
    assert "x-weird: okvalue" in patterns
    assert all(
        character.isprintable() or character == " " for p in patterns for character in p
    )


def test_a_hostile_inline_script_cannot_stall_the_parser() -> None:
    # An unbounded lazy scan over a large crafted script is a denial of
    # service; every quantifier in the literal search is bounded.
    body = (
        "<html><head><script>"
        + ("Shopify.shop = " + "{" * 5000)
        + "</script></head></html>"
    )
    profile = profile_from_page(
        url="https://slow.example/",
        final_url="https://slow.example/",
        status=200,
        headers={},
        cookie_names=[],
        body=body,
    )
    assert profile.status == 200


def test_a_truncated_read_says_so() -> None:
    # A real storefront (milton.in, 3.25 MB on 2026-09-10) exceeded the first
    # ceiling we picked. Dropping the tail is fine; dropping it silently is
    # not, because a later stage would read the missing part as absence.
    profile = profile_from_page(
        url="https://huge.example/",
        final_url="https://huge.example/",
        status=200,
        headers={},
        cookie_names=[],
        body="<html><head><title>Cut short</title></head></html>",
        size_bytes=8 * 1024 * 1024,
        truncated=True,
    )
    assert profile.truncated is True
    assert summarize(profile)["truncated"] is True


def test_a_complete_read_is_not_marked_truncated() -> None:
    profile = _profile("plain_home.html", url="https://nilgiri-roasters.example/")
    assert profile.truncated is False
    assert summarize(profile)["truncated"] is False

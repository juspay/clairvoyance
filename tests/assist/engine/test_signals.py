"""Matching a fingerprint against an observation, and refusing a forged one.

A probe reads a page that anyone can write. Whatever it concludes decides
which adapter handles the site and, further down the pipeline, which tenant
it is. So a site must not be able to make itself look like a platform it is
not just by choosing what to put on the page.
"""

from __future__ import annotations

import pytest

from app.ai.voice.agents.breeze_buddy.assist.engine.classify.signals import (
    signal_matches,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.models import Signal


def _signal(kind: str, pattern: str) -> Signal:
    return Signal(kind=kind, pattern=pattern)


def test_a_host_matches_itself_and_its_subdomains() -> None:
    assert signal_matches(
        _signal("script_src", "cdn.example.com"), "script_src", "cdn.example.com"
    )
    assert signal_matches(
        _signal("script_src", "assets.cdn.example.com"), "script_src", "cdn.example.com"
    )
    assert signal_matches(
        _signal("script_src", "CDN.Example.COM"), "script_src", "cdn.example.com"
    )


@pytest.mark.parametrize(
    "observed",
    [
        # The forgery this replaced a substring test to stop: any site can
        # load a script from a host that merely starts with the real one.
        "cdn.example.com.attacker.net",
        "cdn.example.com.evil",
        "notcdn.example.com",
        "cdn-example.com",
        "example.com",
    ],
)
def test_a_host_that_only_looks_similar_does_not_match(observed: str) -> None:
    assert not signal_matches(
        _signal("script_src", observed), "script_src", "cdn.example.com"
    )


def test_a_header_matches_by_name_then_value() -> None:
    assert signal_matches(
        _signal("header", "powered-by: acme"), "header", "powered-by: acme"
    )
    # Servers append versions and comments; the fingerprint is still there.
    assert signal_matches(
        _signal("header", "powered-by: acme 4.1 (edge)"), "header", "powered-by: acme"
    )


@pytest.mark.parametrize(
    "observed",
    [
        # A site can name its own headers and fill its own values, so the
        # fingerprint has to be anchored to the header it belongs to.
        "x-custom: powered-by: acme",
        "x-powered-by: acme",
        "powered-by-x: acme",
    ],
)
def test_a_header_value_planted_elsewhere_does_not_match(observed: str) -> None:
    assert not signal_matches(_signal("header", observed), "header", "powered-by: acme")


@pytest.mark.parametrize("kind", ["cookie_key", "meta", "js_literal"])
def test_named_things_match_exactly(kind: str) -> None:
    assert signal_matches(_signal(kind, "_acme_y"), kind, "_acme_y")
    assert not signal_matches(_signal(kind, "_acme_y_extra"), kind, "_acme_y")
    assert not signal_matches(_signal(kind, "x_acme_y"), kind, "_acme_y")


def test_free_text_markers_still_match_anywhere() -> None:
    # A marker inside a script body is a fragment by nature — appearing
    # anywhere in the text is the whole point of looking for it.
    assert signal_matches(
        _signal("js_global", "var base='/cdn/shop/files'"), "js_global", "/cdn/shop/"
    )


def test_a_signal_of_another_kind_never_matches() -> None:
    assert not signal_matches(
        _signal("meta", "cdn.example.com"), "script_src", "cdn.example.com"
    )


def test_an_empty_fingerprint_matches_nothing() -> None:
    assert not signal_matches(
        _signal("script_src", "cdn.example.com"), "script_src", "  "
    )

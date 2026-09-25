"""The store's published policies: which host is asked, and what comes back."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from app.ai.voice.agents.breeze_buddy.assist.engine.models import SiteProfile
from app.ai.voice.agents.breeze_buddy.assist.platforms import registry
from app.ai.voice.agents.breeze_buddy.assist.platforms.shopify import documents


class _Result:
    """The part of FetchResult that the policy reader uses."""

    def __init__(self, body: str, *, status: int = 200, truncated: bool = False):
        self.body = body
        self.status = status
        self.truncated = truncated


def _answer(monkeypatch, body: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    text = body if isinstance(body, str) else json.dumps(body)

    async def fetch(url: str, **options: Any):
        calls.append({"url": url, **options})
        return _Result(text, **kwargs)

    monkeypatch.setattr(documents, "fetch_page", fetch)
    return calls


def _profile(**inline: str) -> SiteProfile:
    return SiteProfile(
        url="https://www.brand.example",
        final_url="https://www.brand.example/",
        status=200,
        inline_literals=inline,
    )


SHOP = {
    "data": {
        "shop": {
            "shippingPolicy": {
                "title": "Shipping",
                "url": "https://checkout.example/policies/1",
                "body": " Ships in 2 days. ",
            },
            "refundPolicy": {
                "title": "",
                "url": "https://checkout.example/policies/2",
                "body": "",
            },
            "privacyPolicy": None,
            "termsOfService": {"title": "Terms", "url": ""},
        }
    }
}


# ── which host is asked ──────────────────────────────────────────────────────


async def test_the_validated_permanent_domain_is_asked(monkeypatch) -> None:
    calls = _answer(monkeypatch, SHOP)
    shopify = registry.resolve("shopify")
    await shopify.known_documents(_profile(**{"Shopify.shop": "brand.myshopify.com"}))
    assert calls[0]["url"] == "https://brand.myshopify.com/api/2025-01/graphql.json"


async def test_a_page_literal_that_is_not_a_permanent_domain_is_never_asked(
    monkeypatch,
) -> None:
    # Page text chooses no destinations: an arbitrary host in the literal
    # falls back to the host the probe already fetched.
    calls = _answer(monkeypatch, SHOP)
    shopify = registry.resolve("shopify")
    await shopify.known_documents(_profile(**{"Shopify.shop": "internal.evil.example"}))
    assert calls[0]["url"].startswith("https://www.brand.example/")


async def test_a_plain_website_has_no_known_documents(monkeypatch) -> None:
    calls = _answer(monkeypatch, SHOP)
    assert await registry.resolve("generic").known_documents(_profile()) == ()
    assert calls == []


# ── what comes back ──────────────────────────────────────────────────────────


async def test_published_policies_become_documents(monkeypatch) -> None:
    calls = _answer(monkeypatch, SHOP)
    found = await documents.known_documents("brand.myshopify.com", "www.brand.example")

    assert [(d.kind, d.title, d.body) for d in found] == [
        ("delivery", "Shipping", "Ships in 2 days."),
        ("returns", "returns", None),
    ]
    assert found[0].display_url == "https://www.brand.example/policies/shipping-policy"
    assert calls[0]["json_body"] == {"query": documents.QUERY}
    assert calls[0]["max_redirects"] == 0
    assert calls[0]["max_bytes"] == documents.MAX_RESPONSE_BYTES


@pytest.mark.parametrize(
    "body, options",
    [
        ("not json", {}),
        ([], {}),
        ({"data": []}, {}),
        ({"data": {"shop": None}}, {}),
        ({"errors": [{"message": "no"}]}, {}),
        (SHOP, {"status": 403}),
        (SHOP, {"truncated": True}),
        ("[" * 100_000, {}),
    ],
)
async def test_an_unusable_answer_is_no_documents(monkeypatch, body, options) -> None:
    _answer(monkeypatch, body, **options)
    assert await documents.known_documents("brand.myshopify.com") == ()


async def test_an_unreachable_api_is_no_documents(monkeypatch) -> None:
    async def fetch(url: str, **_: Any):
        raise documents.FetchFailedError("timed out")

    monkeypatch.setattr(documents, "fetch_page", fetch)
    assert await documents.known_documents("brand.myshopify.com") == ()


async def test_an_unexpected_failure_is_no_documents(monkeypatch) -> None:
    async def fetch(url: str, **_: Any):
        raise IndexError("string index out of range")

    monkeypatch.setattr(documents, "fetch_page", fetch)
    assert await documents.known_documents("brand.myshopify.com") == ()


async def test_unguarded_egress_is_not_swallowed(monkeypatch) -> None:
    async def fetch(url: str, **_: Any):
        raise documents.EgressNotGuardedError("proxied")

    monkeypatch.setattr(documents, "fetch_page", fetch)
    with pytest.raises(documents.EgressNotGuardedError):
        await documents.known_documents("brand.myshopify.com")


async def test_an_ipv6_host_is_never_asked(monkeypatch) -> None:
    calls = _answer(monkeypatch, SHOP)
    profile = SiteProfile(
        url="https://[2606:4700:4700::1111]/",
        final_url="https://[2606:4700:4700::1111]/",
        status=200,
    )
    assert await registry.resolve("shopify").known_documents(profile) == ()
    assert calls == []

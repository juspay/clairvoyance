"""Store research over Firecrawl: which pages are read, what becomes a note,
and how a run ends. No network."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple

import pytest

from app.ai.voice.agents.breeze_buddy.assist.engine.research import site
from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research.providers import (
    firecrawl_site,
)

HOME = "https://store.example"


def _firecrawl(
    monkeypatch,
    *,
    links: List[str] | Exception,
    pages: Dict[str, Any],
) -> List[str]:
    """Fake map and scrape; ``pages`` maps an address to its filled json, an
    exception, or (final address, json). Returns the addresses scraped."""
    scraped: List[str] = []

    async def map_site(url: str, **_: Any) -> List[str]:
        if isinstance(links, Exception):
            raise links
        return links

    async def scrape_json(url: str, **_: Any) -> Tuple[str, Dict[str, Any]]:
        scraped.append(url)
        page = pages[url]
        if isinstance(page, Exception):
            raise page
        if isinstance(page, tuple):
            return page
        return url, page

    monkeypatch.setattr(firecrawl_site, "map_site", map_site)
    monkeypatch.setattr(firecrawl_site, "scrape_json", scrape_json)
    return scraped


def test_the_home_page_and_one_page_per_kind_are_picked() -> None:
    links = [
        f"{HOME}/collections/all",
        f"{HOME}/pages/about-us",
        f"{HOME}/blogs/news/about-our-new-range",
        f"{HOME}/pages/contact",
        f"{HOME}/policies/shipping-policy",
        f"{HOME}/policies/refund-policy",
        "https://elsewhere.example/pages/faq",
        f"http://{HOME[8:]}/pages/faq",
        f"https://shop.{HOME[8:]}/help",
    ]
    assert site.pick_pages(HOME, links) == [
        HOME,
        f"{HOME}/pages/about-us",
        f"{HOME}/pages/contact",
        f"https://shop.{HOME[8:]}/help",
        f"{HOME}/policies/shipping-policy",
        f"{HOME}/policies/refund-policy",
    ]


@pytest.mark.parametrize(
    "other, same",
    [
        ("https://www.store.example/a", True),
        ("https://shop.store.example/a", True),
        ("https://store.example.evil/a", False),
        ("https://notstore.example/a", False),
    ],
)
def test_same_site(other: str, same: bool) -> None:
    assert site.same_site("https://www.store.example", other) is same


async def test_facts_stream_as_notes_once_each(monkeypatch) -> None:
    _firecrawl(
        monkeypatch,
        links=[f"{HOME}/pages/faq"],
        pages={
            HOME: {"email": ["care@store.example"], "tagline": "Made slowly"},
            f"{HOME}/pages/faq": {
                "email": ["Care@store.example ", ""],
                "tagline": ["Made slowly."],
                "faq": ["Q: COD? A: Yes"],
                "not_a_field": ["x"],
            },
        },
    )
    events: List[Tuple[str, Dict[str, Any]]] = []

    async def on_event(kind: str, data: Dict[str, Any]) -> None:
        events.append((kind, data))

    result = await site.research(HOME, on_event=on_event)
    assert result.pages_read == 2
    assert result.stopped_because == "finished"
    assert {(n.field, n.value) for n in result.notes} == {
        ("email", "care@store.example"),
        ("tagline", "Made slowly"),
        ("faq", "Q: COD? A: Yes"),
    }
    notes = [data for kind, data in events if kind == "note"]
    assert len(notes) == 3
    assert {"field", "value", "source_url"} == set(notes[0])


async def test_a_linked_page_that_lands_off_the_site_gives_no_notes(
    monkeypatch,
) -> None:
    _firecrawl(
        monkeypatch,
        links=[f"{HOME}/pages/contact"],
        pages={
            HOME: {},
            f"{HOME}/pages/contact": (
                "https://helpdesk.example/contact",
                {"email": ["help@helpdesk.example"]},
            ),
        },
    )
    result = await site.research(HOME)
    assert result.notes == []


async def test_without_a_map_the_home_page_is_still_read(monkeypatch) -> None:
    scraped = _firecrawl(
        monkeypatch,
        links=WebsiteScrapingUpstreamError("map failed"),
        pages={HOME: {"tagline": ["Made slowly"]}},
    )
    result = await site.research(HOME)
    assert scraped == [HOME]
    assert [n.value for n in result.notes] == ["Made slowly"]


async def test_no_page_read_is_an_error(monkeypatch) -> None:
    _firecrawl(
        monkeypatch,
        links=[],
        pages={HOME: WebsiteScrapingUpstreamError("blocked")},
    )
    with pytest.raises(WebsiteScrapingUpstreamError):
        await site.research(HOME)


async def test_no_key_is_a_configuration_error(monkeypatch) -> None:
    _firecrawl(
        monkeypatch,
        links=WebsiteScrapingConfigurationError("no key"),
        pages={},
    )
    with pytest.raises(WebsiteScrapingConfigurationError):
        await site.research(HOME)


async def test_the_budget_ends_a_slow_run_with_what_was_found(monkeypatch) -> None:
    _firecrawl(monkeypatch, links=[f"{HOME}/pages/faq"], pages={})

    async def scrape_json(url: str, **_: Any) -> Tuple[str, Dict[str, Any]]:
        if url != HOME:
            await asyncio.sleep(3600)
        return url, {"tagline": ["Made slowly"]}

    monkeypatch.setattr(firecrawl_site, "scrape_json", scrape_json)
    monkeypatch.setattr(site, "MAX_SECONDS", 0.2)
    result = await site.research(HOME)
    assert result.stopped_because == "out_of_time"
    assert [n.value for n in result.notes] == ["Made slowly"]


async def test_firecrawl_requests_match_its_v2_api(monkeypatch) -> None:
    sent: List[Tuple[str, Dict[str, Any]]] = []

    async def post(endpoint: str, payload: Dict[str, Any], _: float) -> Dict[str, Any]:
        sent.append((endpoint, payload))
        if endpoint == firecrawl_site.MAP_ENDPOINT:
            return {"success": True, "links": [{"url": f"{HOME}/a"}, {"title": "x"}]}
        return {
            "success": True,
            "data": {"json": {"tagline": ["Hi"]}, "metadata": {"url": f"{HOME}/"}},
        }

    monkeypatch.setattr(firecrawl_site, "_post", post)
    assert await firecrawl_site.map_site(HOME, limit=5, timeout_seconds=2) == [
        f"{HOME}/a"
    ]
    assert await firecrawl_site.scrape_json(
        HOME, schema=site.SCHEMA, prompt="p", timeout_seconds=2
    ) == (f"{HOME}/", {"tagline": ["Hi"]})
    (_, map_body), (_, scrape_body) = sent
    assert map_body["limit"] == 5 and map_body["timeout"] == 2000
    assert scrape_body["formats"] == [
        {"type": "json", "schema": site.SCHEMA, "prompt": "p"}
    ]
    assert scrape_body["onlyMainContent"] is False

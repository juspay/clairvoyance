"""Store research over Firecrawl: which pages are read, what becomes a note,
and how a run ends. No network."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple

import pytest

from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUnavailableError,
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research.providers import (
    firecrawl,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding.research import (
    prompts,
    service,
    utils,
)
from app.schemas.breeze_buddy.assist.onboarding.research import (
    AssistResearchCompletion,
)

HOME = "https://store.example"


async def _ignore(kind: str, data: Dict[str, Any]) -> None:
    return None


async def _read() -> Tuple[AssistResearchCompletion, List[Tuple[str, str]]]:
    """Run research on HOME; its result and the (field, value) notes streamed."""
    notes: List[Tuple[str, str]] = []

    async def on_event(kind: str, data: Dict[str, Any]) -> None:
        if kind == "note":
            notes.append((data["field"], data["value"]))

    return await service.read_facts(HOME, on_event=on_event), notes


def _firecrawl(
    monkeypatch,
    *,
    links: List[str] | Exception,
    pages: Dict[str, Any],
    home_links: List[str] = [],
) -> List[str]:
    """Fake map and scrape; ``pages`` maps an address to its filled json, an
    exception, or (final address, json). The home page links to
    ``home_links``. Returns the addresses scraped."""
    scraped: List[str] = []

    async def list_pages(url: str, **_: Any) -> List[str]:
        if isinstance(links, Exception):
            raise links
        return links

    async def read_page(url: str, **_: Any) -> Tuple[str, Dict[str, Any], List[str]]:
        scraped.append(url)
        page = pages[url]
        if isinstance(page, Exception):
            raise page
        found = home_links if url == HOME else []
        if isinstance(page, tuple):
            return (*page, found)
        return url, page, found

    monkeypatch.setattr(firecrawl, "list_pages", list_pages)
    monkeypatch.setattr(firecrawl, "read_page", read_page)
    return scraped


def test_one_page_per_kind_is_picked() -> None:
    links = [
        f"{HOME}/pages/about-us",
        f"{HOME}/blogs/news/about-our-new-range",
        f"{HOME}/pages/contact",
        f"{HOME}/policies/shipping-policy",
        f"{HOME}/policies/refund-policy",
        "https://elsewhere.example/pages/faq",
        f"http://{HOME[8:]}/pages/faq",
        f"https://shop.{HOME[8:]}/help",
    ]
    assert utils.pick_pages(HOME, [], links) == [
        f"{HOME}/pages/about-us",
        f"{HOME}/pages/contact",
        f"https://shop.{HOME[8:]}/help",
        f"{HOME}/policies/shipping-policy",
        f"{HOME}/policies/refund-policy",
    ]


def test_app_and_blog_pages_are_never_picked() -> None:
    links = [f"{HOME}/apps/returns", f"{HOME}/blogs/news/our-story"]
    assert utils.pick_pages(HOME, [], links) == []


def test_a_big_pages_folder_keeps_its_store_pages() -> None:
    # allbirds.com: /pages/ holds 50+ marketing pages and its about, contact
    # and help pages; none of them is a listing.
    pages = [f"{HOME}/pages/campaign-{n}" for n in range(60)]
    links = pages + [f"{HOME}/pages/our-story", f"{HOME}/pages/help"]
    assert utils.pick_pages(HOME, [], links) == [
        f"{HOME}/pages/our-story",
        f"{HOME}/pages/help",
    ]


def test_the_page_the_home_page_links_beats_a_shorter_one() -> None:
    # allbirds.com: its menu links "Returns & Exchanges", the map also lists
    # Shopify's own refund policy page.
    assert utils.pick_pages(
        HOME,
        [f"{HOME}/pages/returns-exchanges"],
        [f"{HOME}/policies/refund-policy"],
    ) == [f"{HOME}/pages/returns-exchanges"]


def test_the_home_page_links_come_before_the_map() -> None:
    assert utils.pick_pages(
        HOME, [f"{HOME}/about-us/"], [f"{HOME}/about", f"{HOME}/contact"]
    ) == [f"{HOME}/about-us/", f"{HOME}/contact"]


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
    assert utils.same_site("https://www.store.example", other) is same


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

    result = await service.read_facts(HOME, on_event=on_event)
    assert result.status == "completed"
    notes = [data for kind, data in events if kind == "note"]
    assert {(n["field"], n["value"]) for n in notes} == {
        ("email", "care@store.example"),
        ("tagline", "Made slowly"),
        ("faq", "Q: COD? A: Yes"),
    }
    assert len(notes) == 3
    assert {"field", "value", "source_url"} == set(notes[0])


async def test_contacts_that_are_only_links_are_read_from_the_links(
    monkeypatch,
) -> None:
    _firecrawl(
        monkeypatch,
        links=[],
        pages={HOME: {}},
        home_links=[
            "https://wa.me/919773581254",
            "https://api.whatsapp.com/send?phone=91%2098765%2043210",
            "mailto:care@store.example",
            "tel:+911234567890",
        ],
    )
    _, notes = await _read()
    assert notes == [
        ("whatsapp", "+919773581254"),
        ("whatsapp", "+919876543210"),
        ("email", "care@store.example"),
    ]


async def test_without_a_map_the_home_page_is_still_read(monkeypatch) -> None:
    scraped = _firecrawl(
        monkeypatch,
        links=WebsiteScrapingUpstreamError("map failed"),
        pages={HOME: {"tagline": ["Made slowly"]}},
    )
    _, notes = await _read()
    assert scraped == [HOME]
    assert notes == [("tagline", "Made slowly")]


async def test_no_page_read_is_an_error(monkeypatch) -> None:
    _firecrawl(
        monkeypatch,
        links=[],
        pages={HOME: WebsiteScrapingUpstreamError("blocked")},
    )
    with pytest.raises(WebsiteScrapingUpstreamError):
        await service.read_facts(HOME, on_event=_ignore)


async def test_a_busy_provider_is_told_apart_from_an_unreadable_site(
    monkeypatch,
) -> None:
    _firecrawl(
        monkeypatch,
        links=[f"{HOME}/pages/faq"],
        pages={
            HOME: WebsiteScrapingUnavailableError("provider returned 429"),
            f"{HOME}/pages/faq": WebsiteScrapingUpstreamError("blocked"),
        },
    )
    with pytest.raises(WebsiteScrapingUnavailableError):
        await service.read_facts(HOME, on_event=_ignore)


async def test_no_key_is_a_configuration_error(monkeypatch) -> None:
    _firecrawl(
        monkeypatch,
        links=WebsiteScrapingConfigurationError("no key"),
        pages={HOME: WebsiteScrapingConfigurationError("no key")},
    )
    with pytest.raises(WebsiteScrapingConfigurationError):
        await service.read_facts(HOME, on_event=_ignore)


async def test_the_budget_ends_a_slow_run_with_what_was_found(monkeypatch) -> None:
    _firecrawl(monkeypatch, links=[f"{HOME}/pages/faq"], pages={})

    async def read_page(url: str, **_: Any) -> Tuple[str, Dict[str, Any], List[str]]:
        if url != HOME:
            await asyncio.sleep(3600)
        return url, {"tagline": ["Made slowly"]}, []

    monkeypatch.setattr(firecrawl, "read_page", read_page)
    monkeypatch.setattr(service, "_MAX_SECONDS", 0.2)
    result, notes = await _read()
    assert result.status == "timed_out"
    assert notes == [("tagline", "Made slowly")]


async def test_firecrawl_requests_match_its_v2_api(monkeypatch) -> None:
    sent: List[Tuple[str, Dict[str, Any]]] = []

    async def post(endpoint: str, payload: Dict[str, Any], _: float) -> Dict[str, Any]:
        sent.append((endpoint, payload))
        if endpoint == firecrawl._MAP_ENDPOINT:
            return {"success": True, "links": [{"url": f"{HOME}/a", "title": "x"}]}
        return {
            "success": True,
            "data": {
                "json": {"tagline": ["Hi"]},
                "links": [f"{HOME}/a"],
                "metadata": {"url": f"{HOME}/"},
            },
        }

    monkeypatch.setattr(firecrawl, "_call_firecrawl", post)
    assert await firecrawl.list_pages(HOME, timeout_seconds=2) == [f"{HOME}/a"]
    assert await firecrawl.read_page(
        HOME, schema=prompts.SCHEMA, prompt="p", timeout_seconds=2, whole_page=True
    ) == (f"{HOME}/", {"tagline": ["Hi"]}, [f"{HOME}/a"])
    await firecrawl.read_page(
        HOME, schema=prompts.SCHEMA, prompt="p", timeout_seconds=2
    )
    (_, map_body), (_, home_body), (_, page_body) = sent
    assert "limit" not in map_body and map_body["timeout"] == 2000
    assert home_body["formats"] == [
        {"type": "json", "schema": prompts.SCHEMA, "prompt": "p"},
        "links",
    ]
    assert home_body["onlyMainContent"] is False
    assert page_body["onlyMainContent"] is True
    # Only the home page's links are used, so only it asks for them.
    assert page_body["formats"] == [
        {"type": "json", "schema": prompts.SCHEMA, "prompt": "p"}
    ]


@pytest.mark.parametrize(
    "status, error",
    [
        (402, WebsiteScrapingUnavailableError),
        (429, WebsiteScrapingUnavailableError),
        (503, WebsiteScrapingUnavailableError),
        (403, WebsiteScrapingUpstreamError),
        (401, WebsiteScrapingConfigurationError),
    ],
)
async def test_firecrawl_statuses_that_say_nothing_about_the_site_are_retryable(
    monkeypatch, status, error
) -> None:
    class _Response:
        async def __aenter__(self) -> "_Response":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

    _Response.status = status

    class _Session:
        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        def post(self, *_: Any, **__: Any) -> _Response:
            return _Response()

    monkeypatch.setattr(firecrawl, "FIRECRAWL_API_KEY", "key")
    monkeypatch.setattr(firecrawl, "create_aiohttp_session", lambda **_: _Session())
    with pytest.raises(error) as raised:
        await firecrawl.list_pages(HOME, timeout_seconds=2)
    assert type(raised.value) is error


def test_files_are_never_picked_as_pages() -> None:
    # Seen on kosha.co.in and allbirds.com site maps, 7 Oct 2026.
    links = [
        f"{HOME}/pdf/investors/annual-returns/Annual%20Return%202022.pdf",
        f"{HOME}/cdn/shop/t/4159/assets/_commonjsHelpers.js",
        f"{HOME}/policies/refund-policy",
        f"{HOME}/about-us.html",
    ]
    assert utils.pick_pages(HOME, [], links) == [
        f"{HOME}/about-us.html",
        f"{HOME}/policies/refund-policy",
    ]


def test_a_page_with_a_trailing_slash_is_picked_once() -> None:
    assert utils.pick_pages(
        HOME,
        [f"{HOME}/pages/shipping-returns"],
        [f"{HOME}/pages/shipping-returns/", f"{HOME}/pages/shipping-returns#faq"],
    ) == [f"{HOME}/pages/shipping-returns"]


def test_a_small_stores_item_pages_are_never_picked() -> None:
    # 30 items is no "listing" by size, but the platform names the folders.
    items = [f"{HOME}/products/item-{n}" for n in range(30)]
    links = items + [
        f"{HOME}/collections/help-me-choose",
        f"{HOME}/products/express-delivery-pouch",
    ]
    assert utils.pick_pages(HOME, links, []) == []


async def test_the_contact_a_page_states_beats_a_footer_link(monkeypatch) -> None:
    _firecrawl(
        monkeypatch,
        links=[],
        pages={
            HOME: {"email": ["support@store.example"], "whatsapp": ["+91 98765 43210"]}
        },
        home_links=["mailto:hello@pixelagency.com", "https://wa.me/919999900000"],
    )
    _, notes = await _read()
    assert notes == [
        ("whatsapp", "+91 98765 43210"),
        ("email", "support@store.example"),
    ]


async def test_pages_that_fail_do_not_end_the_run(monkeypatch) -> None:
    pages = [f"{HOME}/pages/{kind}" for kind in ("about", "faq", "shipping")]
    _firecrawl(
        monkeypatch,
        links=pages,
        pages={
            HOME: {"tagline": ["Made slowly"]},
            **{page: WebsiteScrapingUnavailableError("429") for page in pages},
        },
    )
    result, notes = await _read()
    assert result.status == "completed"
    assert notes == [("tagline", "Made slowly")]


async def test_a_busy_provider_on_the_home_page_stops_the_run(monkeypatch) -> None:
    scraped = _firecrawl(
        monkeypatch,
        links=[f"{HOME}/pages/faq"],
        pages={
            HOME: WebsiteScrapingUnavailableError("402"),
            f"{HOME}/pages/faq": {"faq": ["Q: x A: y"]},
        },
    )
    with pytest.raises(WebsiteScrapingUnavailableError):
        await service.read_facts(HOME, on_event=_ignore)
    assert scraped == [HOME]


@pytest.mark.parametrize(
    "data",
    [{"json": None}, {"json": ["x"]}, {"json": "x"}, None],
)
async def test_a_page_firecrawl_could_not_fill_is_one_failed_page(
    monkeypatch, data
) -> None:
    async def post(*_: Any) -> Dict[str, Any]:
        return {"success": True, "data": data}

    monkeypatch.setattr(firecrawl, "_call_firecrawl", post)
    with pytest.raises(WebsiteScrapingUpstreamError):
        await firecrawl.read_page(
            HOME, schema=prompts.SCHEMA, prompt="p", timeout_seconds=2
        )


async def test_null_links_on_the_home_page_are_no_links(monkeypatch) -> None:
    async def post(*_: Any) -> Dict[str, Any]:
        return {"success": True, "data": {"json": {}, "links": None}}

    monkeypatch.setattr(firecrawl, "_call_firecrawl", post)
    assert await firecrawl.read_page(
        HOME, schema=prompts.SCHEMA, prompt="p", timeout_seconds=2, whole_page=True
    ) == (HOME, {}, [])


async def test_odd_links_and_addresses_are_skipped_not_fatal(monkeypatch) -> None:
    async def post(endpoint: str, payload: Dict[str, Any], _: float) -> Dict[str, Any]:
        if endpoint == firecrawl._MAP_ENDPOINT:
            return {"links": [{"url": None}, {"url": 5}, "x", {"url": f"{HOME}/a"}]}
        return {
            "data": {
                "json": {},
                "links": [None, 3, {"url": "x"}, "https://[bad", f"{HOME}/b"],
                "metadata": {"url": 5},
            }
        }

    monkeypatch.setattr(firecrawl, "_call_firecrawl", post)
    assert await firecrawl.list_pages(HOME, timeout_seconds=2) == [f"{HOME}/a"]
    assert await firecrawl.read_page(
        HOME, schema=prompts.SCHEMA, prompt="p", timeout_seconds=2, whole_page=True
    ) == (HOME, {}, [f"{HOME}/b"])


def test_server_pages_count_and_folders_behind_a_language_are_skipped() -> None:
    links = [
        f"{HOME}/pages/returns.php",
        f"{HOME}/contact.aspx",
        f"{HOME}/en-in/products/express-delivery-pouch",
        f"{HOME}/en-in/collections/help-me-choose",
        f"{HOME}/fr/blogs/news/our-story",
    ]
    assert utils.pick_pages(HOME, [], links) == [
        f"{HOME}/contact.aspx",
        f"{HOME}/pages/returns.php",
    ]


@pytest.mark.parametrize("final", ["http://[::1", "   ", 5, "/relative"])
async def test_an_odd_final_address_falls_back_to_the_asked_one(
    monkeypatch, final
) -> None:
    async def post(*_: Any) -> Dict[str, Any]:
        return {"data": {"json": {}, "metadata": {"url": final}}}

    monkeypatch.setattr(firecrawl, "_call_firecrawl", post)
    source, _, _ = await firecrawl.read_page(
        HOME, schema=prompts.SCHEMA, prompt="p", timeout_seconds=2
    )
    assert source == HOME


async def test_a_punctuation_only_value_is_no_fact(monkeypatch) -> None:
    _firecrawl(monkeypatch, links=[], pages={HOME: {"brand_line": ["...", "-"]}})
    _, notes = await _read()
    assert notes == []

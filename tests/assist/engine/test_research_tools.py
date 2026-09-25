"""The researcher's tools: what they read, what they refuse, what they cost."""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List

import pytest
import regex

from app.ai.voice.agents.breeze_buddy.assist.engine.research import tools


def page(url: str, text: str = "", *, status: int = 200, size: int = 0) -> tools.Page:
    return tools.Page(
        url=url,
        status=status,
        content_type="text/html",
        text=text,
        size_bytes=size or len(text),
    )


class _Result:
    """The part of FetchResult that read_pages uses."""

    def __init__(self, final_url: str, body: str = "<html>hi</html>") -> None:
        self.final_url = final_url
        self.status = 200
        self.headers = {"content-type": "text/html"}
        self.body = body
        self.size_bytes = len(body)
        self.truncated = False


def fake_fetch(monkeypatch, calls: List[str], *, final_url=None, error=None):
    async def fetch(url: str, **_: object):
        await asyncio.sleep(0)
        calls.append(url)
        if error:
            raise error
        return _Result(final_url or url)

    monkeypatch.setattr(tools, "fetch_page", fetch)


# ── same site ────────────────────────────────────────────────────────────────


def test_site_of_is_the_host_without_www_or_scheme() -> None:
    assert tools.site_of("https://www.milton.in/x") == "milton.in"
    assert tools.site_of("HTTPS://Shop.Example.COM") == "shop.example.com"
    assert tools.site_of("https://[::1].shop.com/") == ""


def test_same_site_allows_the_host_and_its_subdomains_only() -> None:
    root = "https://x.test"
    assert tools.same_site("https://cdn.x.test/a.js", root)
    assert tools.same_site("https://x.test/a", "https://www.x.test")
    assert not tools.same_site("https://evil.test/", root)
    assert not tools.same_site("https://brandx.test/", root)
    assert not tools.same_site("", root)
    assert not tools.same_site("https://x.test/a", "https://[x")


def test_another_store_on_a_shared_domain_is_not_the_same_site() -> None:
    assert not tools.same_site(
        "https://beta.myshopify.com/", "https://alpha.myshopify.com"
    )
    assert not tools.same_site("https://other.co.in/", "https://brand.co.in")


# ── read_pages ───────────────────────────────────────────────────────────────


async def test_off_site_urls_are_skipped(monkeypatch) -> None:
    calls: List[str] = []
    fake_fetch(monkeypatch, calls)
    evidence = tools.Evidence(root="https://x.test")
    await tools.read_pages(["https://x.test/a", "https://elsewhere.test/b"], evidence)
    assert calls == ["https://x.test/a"]
    assert any("skipped off-site" in step for step in evidence.steps)


async def test_an_unparseable_url_does_not_lose_the_rest(monkeypatch) -> None:
    calls: List[str] = []
    fake_fetch(monkeypatch, calls)
    evidence = tools.Evidence(root="https://x.test")
    pages = await tools.read_pages(
        ["https://[::1].shop.com/", "https://x.test/a"], evidence
    )
    assert [p.url for p in pages] == ["https://x.test/a"]


async def test_a_failed_read_is_a_page_not_an_exception(monkeypatch) -> None:
    fake_fetch(monkeypatch, [], error=tools.UnsafeUrlError("blocked"))
    pages = await tools.read_pages(
        ["https://x.test/a"], tools.Evidence(root="https://x.test")
    )
    assert pages[0].ok is False and pages[0].error == "blocked"


async def test_unguarded_egress_stops_the_run(monkeypatch) -> None:
    fake_fetch(monkeypatch, [], error=tools.EgressNotGuardedError("proxied"))
    with pytest.raises(tools.EgressNotGuardedError):
        await tools.read_pages(
            ["https://x.test/a"], tools.Evidence(root="https://x.test")
        )


async def test_a_redirect_off_site_is_dropped(monkeypatch) -> None:
    fake_fetch(monkeypatch, [], final_url="https://elsewhere.test/landing")
    pages = await tools.read_pages(
        ["https://x.test/go"], tools.Evidence(root="https://x.test")
    )
    assert pages[0].error == "redirected off-site" and pages[0].text == ""


async def test_a_redirect_within_the_site_is_kept(monkeypatch) -> None:
    fake_fetch(monkeypatch, [], final_url="https://www.x.test/landing")
    pages = await tools.read_pages(
        ["https://x.test/go"], tools.Evidence(root="https://x.test")
    )
    assert pages[0].ok and pages[0].url == "https://www.x.test/landing"


async def test_a_url_is_never_read_twice_even_after_a_redirect(monkeypatch) -> None:
    calls: List[str] = []
    fake_fetch(monkeypatch, calls, final_url="https://x.test/new")
    evidence = tools.Evidence(root="https://x.test")
    await tools.read_pages(["https://x.test/old", "https://x.test/old"], evidence)
    await tools.read_pages(["https://x.test/old", "https://x.test/new"], evidence)
    assert calls == ["https://x.test/old"]
    assert evidence.reads == 1


async def test_reads_are_capped_per_call_and_per_run(monkeypatch) -> None:
    calls: List[str] = []
    fake_fetch(monkeypatch, calls)
    evidence = tools.Evidence(root="https://x.test")
    for batch in range(4):
        urls = [
            f"https://x.test/{batch}/{n}" for n in range(tools.MAX_READS_PER_CALL + 5)
        ]
        await tools.read_pages(urls, evidence)
    assert len(calls) == tools.MAX_READS_PER_RUN == evidence.reads
    assert any("budget spent" in step for step in evidence.steps)


async def test_concurrent_calls_cannot_overspend_or_read_twice(monkeypatch) -> None:
    calls: Dict[str, int] = {}

    async def fetch(url: str, **_: object):
        await asyncio.sleep(0)
        calls[url] = calls.get(url, 0) + 1
        return _Result(url)

    monkeypatch.setattr(tools, "fetch_page", fetch)
    evidence = tools.Evidence(root="https://x.test")
    batches = [
        [f"https://x.test/{n}" for n in range(tools.MAX_READS_PER_CALL)]
        for _ in range(3)
    ]
    await asyncio.gather(*(tools.read_pages(urls, evidence) for urls in batches))
    assert all(count == 1 for count in calls.values())
    assert evidence.reads == len(calls) == tools.MAX_READS_PER_CALL


async def test_each_read_is_capped_at_the_page_limit(monkeypatch) -> None:
    asked: Dict[str, object] = {}

    async def fetch(url: str, **kwargs: object):
        asked.update(kwargs)
        return _Result(url)

    monkeypatch.setattr(tools, "fetch_page", fetch)
    await tools.read_pages(["https://x.test/a"], tools.Evidence(root="https://x.test"))
    assert asked["max_bytes"] == tools.MAX_PAGE_BYTES == 4 * 1024 * 1024
    assert asked["timeout_seconds"] == tools.READ_TIMEOUT_SECONDS == 20.0


async def test_cancelling_keeps_finished_reads_and_returns_the_rest(
    monkeypatch,
) -> None:
    slow_started = asyncio.Event()

    async def fetch(url: str, **_: object):
        if url.endswith("/slow"):
            slow_started.set()
            await asyncio.sleep(10)
        return _Result(url)

    monkeypatch.setattr(tools, "fetch_page", fetch)
    evidence = tools.Evidence(root="https://x.test")
    urls = [f"https://x.test/{n}" for n in range(5)] + ["https://x.test/slow"]
    task = asyncio.ensure_future(tools.read_pages(urls, evidence))
    await slow_started.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert evidence.reads == 5
    assert set(evidence.pages) == set(urls[:5])
    assert not evidence.seen("https://x.test/slow")


# ── find_text ────────────────────────────────────────────────────────────────


async def test_find_text_returns_the_passage_and_its_page() -> None:
    pages = [page("https://x.test/a", "Welcome. Free\n  shipping on orders over 999.")]
    found = await tools.find_text("FREE   shipping", pages)
    assert len(found) == 1
    assert "Free shipping on orders over 999" in found[0].text
    assert found[0].source_url == "https://x.test/a"


async def test_find_text_keeps_the_first_page_a_passage_appeared_on() -> None:
    pages = [
        page("https://x.test/a", "hello@x.test"),
        page("https://x.test/b", "hello@x.test"),
    ]
    found = await tools.find_text("hello@", pages)
    assert [s.source_url for s in found] == ["https://x.test/a"]


async def test_find_text_treats_regex_syntax_as_plain_text() -> None:
    pages = [page("https://x.test/a", "price (a+)+$ here")]
    assert await tools.find_text("(a+)+$", pages)
    assert await tools.find_text("(?x)a{9999}", pages) == []


async def test_find_text_refuses_too_short_and_too_long_phrases() -> None:
    pages = [page("https://x.test/a", "a" * 400)]
    assert await tools.find_text("a", pages) == []
    assert await tools.find_text("a" * (tools.MAX_PHRASE_LENGTH + 1), pages) == []


async def test_find_text_is_cheap_on_a_phrase_repeated_a_million_times() -> None:
    started = time.monotonic()
    await tools.find_text("ab", [page("https://x.test/a", "ab" * 500_000)])
    assert time.monotonic() - started < 0.5


async def test_find_text_handles_text_whose_lowercase_changes_length() -> None:
    assert await tools.find_text(
        "free returns", [page("https://x.test/a", "İstanbul: free returns")]
    )


# ── page_links ───────────────────────────────────────────────────────────────

SHELL = """
<link href="https://x.test/_app/entry/start.AAA.js" rel="modulepreload">
<link href="/_app/entry/app.BBB.js" rel="modulepreload">
<link rel="stylesheet" href="/theme.css">
<script src="/legacy.js"></script>
<a href="/pages/contact">Contact</a><a href="mailto:hi@x.test">Mail</a>
<a href="//[::1">bad</a><img src="/logo.png">
"""


async def test_script_addresses_are_scripts_whatever_declared_them() -> None:
    links = await tools.page_links(page("https://x.test/", SHELL))
    assert links.scripts == [
        "https://x.test/_app/entry/start.AAA.js",
        "https://x.test/_app/entry/app.BBB.js",
        "https://x.test/legacy.js",
    ]
    assert links.pages == ["https://x.test/pages/contact"]


async def test_a_bundle_names_its_siblings_relative_to_itself() -> None:
    bundle = page(
        "https://x.test/_app/entry/start.AAA.js",
        'import("../chunks/CW3.js");const b="../nodes/12.js"',
    )
    links = await tools.page_links(bundle)
    assert links.scripts == [
        "https://x.test/_app/chunks/CW3.js",
        "https://x.test/_app/nodes/12.js",
    ]


async def test_emails_and_phones_come_out_of_anything() -> None:
    bundle = page(
        "https://x.test/_app/nodes/3.js",
        'a="support@x.test",b="first.last+tag@shop.example.co.in",'
        'w="https://wa.me/919876543210"',
    )
    links = await tools.page_links(bundle)
    assert links.emails == ["support@x.test", "first.last+tag@shop.example.co.in"]
    assert links.phones == ["919876543210"]


@pytest.mark.parametrize(
    "text",
    [
        "a" * 1_000_000,
        ("a" * 63 + "@") * (1_000_000 // 64),
        ("a" * 63 + "@b") * (1_000_000 // 65),
        "@" * 1_000_000,
    ],
)
async def test_email_scan_is_cheap_on_a_hostile_megabyte(text) -> None:
    started = time.monotonic()
    await tools.page_links(page("https://x.test/a", text))
    assert time.monotonic() - started < 0.5


# ── readable_lines and client routing ────────────────────────────────────────


async def test_copy_survives_the_filter_and_machinery_does_not() -> None:
    text = (
        '"Breeze Automatic cuts through your business data to serve you '
        'real-time insights, tailored fixes and instant action." '
        '"toolbar-right-content merchant-view" '
        '"position: fixed; bottom: 14px; z-index: 99999" '
        '"M14.16 4.918C11.23 7.048 8.6 9.548 6.35 12.348" '
        '"Save 20% on orders over 2,000 & get free delivery"'
    )
    lines = [
        s.text for s in await tools.readable_lines([page("https://x.test/a", text)])
    ]
    assert any(line.startswith("Breeze Automatic cuts") for line in lines)
    assert any("Save 20%" in line for line in lines)
    assert not any("toolbar-right" in line or "z-index" in line for line in lines)


def test_this_modules_patterns_stop_at_the_timeout() -> None:
    pages = [page(f"https://x.test/{n}", "a" * 5000 + "!") for n in range(10)]
    started = time.monotonic()
    assert tools._match_all(regex.compile(r"((a+)+$)"), pages, limit=10) == []
    assert time.monotonic() - started < tools.PATTERN_TIMEOUT_SECONDS + 1


def test_identical_pages_read_as_client_routed() -> None:
    shell = "<html><body></body></html>"
    assert tools.looks_client_routed(
        [page(f"https://x.test/{n}", shell) for n in "abcd"]
    )


def test_differing_pages_failed_reads_or_two_samples_are_not_client_routed() -> None:
    assert not tools.looks_client_routed(
        [
            page("https://x.test/a", "one"),
            page("https://x.test/b", "longer"),
            page("https://x.test/c", "longest"),
        ]
    )
    assert not tools.looks_client_routed(
        [page(f"https://x.test/{n}", "", status=404) for n in "abc"]
    )
    assert not tools.looks_client_routed(
        [page("https://x.test/a", "s"), page("https://x.test/b", "s")]
    )


# ── evidence ─────────────────────────────────────────────────────────────────


def test_an_empty_note_is_not_recorded() -> None:
    evidence = tools.Evidence(root="https://x.test")
    evidence.note("tagline", "   ", "https://x.test/")
    evidence.note("tagline", "The Commerce Super Stack", "https://x.test/")
    assert [(n.value, n.source_url) for n in evidence.notes] == [
        ("The Commerce Super Stack", "https://x.test/")
    ]


async def test_a_page_landed_on_by_a_redirect_stays_read_after_a_cancel(
    monkeypatch,
) -> None:
    landed = asyncio.Event()

    async def fetch(url: str, **_: object):
        if url.endswith("/a"):
            landed.set()
            return _Result("https://x.test/b")
        await asyncio.sleep(10)
        return _Result(url)

    monkeypatch.setattr(tools, "fetch_page", fetch)
    evidence = tools.Evidence(root="https://x.test")
    task = asyncio.ensure_future(
        tools.read_pages(["https://x.test/a", "https://x.test/b"], evidence)
    )
    await landed.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert evidence.seen("https://x.test/b")
    assert "https://x.test/b" in evidence.pages


async def test_links_per_page_are_capped() -> None:
    text = '<a href="/p">x</a>' * 50_000
    started = time.monotonic()
    await tools.page_links(page("https://x.test/", text))
    assert time.monotonic() - started < 0.5


async def test_reads_refuse_every_off_site_redirect_hop(monkeypatch) -> None:
    asked: Dict[str, object] = {}

    async def fetch(url: str, **kwargs: object):
        asked.update(kwargs)
        return _Result(url)

    monkeypatch.setattr(tools, "fetch_page", fetch)
    await tools.read_pages(["https://x.test/a"], tools.Evidence(root="https://x.test"))
    allow = asked["allow_url"]
    assert callable(allow)
    assert allow("https://www.x.test/next")
    assert not allow("https://elsewhere.test/next")


def test_a_non_ascii_domain_matches_its_fetched_form() -> None:
    assert tools.same_site("https://xn--bcher-kva.de/a", "https://bücher.de")
    assert tools.same_site("https://bücher.de/a", "https://xn--bcher-kva.de")
    assert not tools.same_site("https://xn--bcher-kva.de/a", "https://buecher.de")


def test_truncated_pages_do_not_read_as_client_routed() -> None:
    pages = [
        tools.Page(
            url=f"https://x.test/{n}",
            status=200,
            content_type="text/html",
            text=f"page {n}",
            size_bytes=tools.MAX_PAGE_BYTES,
            truncated=True,
        )
        for n in range(3)
    ]
    assert not tools.looks_client_routed(pages)


async def test_image_names_are_not_emails() -> None:
    text = '<img src="/icon@2x.png"> <img srcset="banner@3x.webp"> hello@x.test'
    links = await tools.page_links(page("https://x.test/", text))
    assert links.emails == ["hello@x.test"]


async def test_css_at_rules_do_not_use_up_the_email_scan() -> None:
    text = "<style>" + "@media(x){}" * 2500 + "</style> mail us at support@x.test"
    links = await tools.page_links(page("https://x.test/", text))
    assert links.emails == ["support@x.test"]


async def test_many_anchors_do_not_crowd_out_scripts() -> None:
    text = "".join(f'<a href="/p{n}">' for n in range(2500))
    text += '<script src="/app.js"></script>'
    links = await tools.page_links(page("https://x.test/", text))
    assert links.scripts == ["https://x.test/app.js"]


async def test_phone_numbers_per_page_are_capped() -> None:
    text = " ".join(f"tel:{n:07d}" for n in range(10_000))
    links = await tools.page_links(page("https://x.test/", text))
    assert len(links.phones) == tools._MAX_LINKS_PER_PAGE


async def test_link_scanning_releases_the_event_loop() -> None:
    text = '<a href="/p">x</a> tel:1234567 ' * 150_000
    longest = 0.0

    async def probe() -> None:
        nonlocal longest
        while True:
            started = time.monotonic()
            await asyncio.sleep(0.005)
            longest = max(longest, time.monotonic() - started - 0.005)

    ticker = asyncio.ensure_future(probe())
    await tools.page_links(page("https://x.test/", text))
    ticker.cancel()
    assert longest < 0.05


def test_a_run_keeps_a_bounded_amount_of_text() -> None:
    evidence = tools.Evidence(root="https://x.test")
    big = "a" * (tools.MAX_TEXT_PER_RUN // 2 + 10)
    for n in range(3):
        evidence.add(page(f"https://x.test/{n}", big))
    assert evidence.kept_chars == tools.MAX_TEXT_PER_RUN
    assert evidence.pages["https://x.test/1"].truncated
    assert evidence.pages["https://x.test/2"].text == ""


async def test_each_read_is_kept_as_it_lands_not_after_the_batch(
    monkeypatch,
) -> None:
    slow_started = asyncio.Event()

    async def fetch(url: str, **_: object):
        if url.endswith("/slow"):
            slow_started.set()
            await asyncio.sleep(10)
        return _Result(url, body="a" * 100)

    monkeypatch.setattr(tools, "fetch_page", fetch)
    monkeypatch.setattr(tools, "MAX_TEXT_PER_RUN", 150)
    evidence = tools.Evidence(root="https://x.test")
    urls = ["https://x.test/1", "https://x.test/2", "https://x.test/slow"]
    task = asyncio.ensure_future(tools.read_pages(urls, evidence))
    await slow_started.wait()
    await asyncio.sleep(0)
    # The slow read is still in flight, yet the finished pages are already
    # held to the run's text cap.
    assert set(evidence.pages) == set(urls[:2])
    assert evidence.kept_chars == 150
    assert evidence.pages["https://x.test/2"].truncated
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("filler", ["a/", "/a.", "a."])
async def test_link_scanning_is_cheap_on_an_unclosed_quote(filler) -> None:
    text = "'" + filler * 200_000
    started = time.monotonic()
    await tools.page_links(page("https://x.test/app.js", text))
    assert time.monotonic() - started < 0.5


def test_every_link_pattern_stops_at_the_timeout(monkeypatch) -> None:
    monkeypatch.setattr(tools, "PATTERN_TIMEOUT_SECONDS", 0.05)
    slow = regex.compile(r"(a+)+b")
    started = time.monotonic()
    assert tools._first_matches(slow, "a" * 40 + "c", 10) == []
    assert time.monotonic() - started < 1.0


async def test_a_redirected_page_is_found_by_the_address_asked_for(
    monkeypatch,
) -> None:
    fake_fetch(monkeypatch, [], final_url="https://www.x.test/landing")
    evidence = tools.Evidence(root="https://x.test")
    await tools.read_pages(["https://x.test/go"], evidence)
    assert evidence.resolve("https://x.test/go") == "https://www.x.test/landing"
    assert evidence.resolve("https://x.test/never") == "https://x.test/never"

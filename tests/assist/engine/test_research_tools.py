"""The researcher's tools: what they read, what they refuse, what they cost."""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List

import pytest

from app.ai.voice.agents.breeze_buddy.assist.engine.research import tools
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import FetchResult


def page(url: str, text: str = "", *, status: int = 200, **kwargs) -> tools.Page:
    return tools.Page(url=url, status=status, text=text, size_bytes=len(text), **kwargs)


def _result(final_url: str, body: str = "<html>hi</html>") -> FetchResult:
    return FetchResult(
        url=final_url,
        final_url=final_url,
        status=200,
        headers={"content-type": "text/html"},
        body=body,
        size_bytes=len(body),
    )


# ── reading ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url, root, same",
    [
        ("https://cdn.x.test/a.js", "https://x.test", True),
        ("https://x.test/a", "https://www.x.test", True),
        ("https://xn--bcher-kva.de/a", "https://bücher.de", True),
        ("https://bücher.de/a", "https://xn--bcher-kva.de", True),
        ("https://evil.test/", "https://x.test", False),
        ("https://brandx.test/", "https://x.test", False),
        ("https://beta.myshopify.com/", "https://alpha.myshopify.com", False),
        ("https://other.co.in/", "https://brand.co.in", False),
        ("https://xn--bcher-kva.de/a", "https://buecher.de", False),
        ("https://[::1].shop.com/", "https://shop.com", False),
        ("", "https://x.test", False),
    ],
)
def test_same_site(url, root, same) -> None:
    assert tools.same_site(url, root) is same


async def test_reads_stay_on_site(monkeypatch) -> None:
    calls: List[str] = []
    asked: Dict[str, object] = {}

    async def fetch(url: str, **kwargs: object):
        calls.append(url)
        asked.update(kwargs)
        return _result(url)

    monkeypatch.setattr(tools, "fetch_page", fetch)
    evidence = tools.Evidence(root="https://x.test")
    await tools.read_pages(
        ["https://x.test/a", "https://elsewhere.test/b", "http://x.test/c"], evidence
    )

    assert calls == ["https://x.test/a"]
    assert evidence.reads == 1
    # Every redirect hop is put to the same rule before it is sent.
    allow = asked["allow_url"]
    assert callable(allow)
    assert allow("https://www.x.test/next")
    assert not allow("https://elsewhere.test/next")
    assert asked["max_bytes"] == tools.MAX_PAGE_BYTES


async def test_a_failed_read_is_a_page_but_unguarded_egress_raises(
    monkeypatch,
) -> None:
    async def blocked(url: str, **_: object):
        raise tools.UnsafeUrlError("blocked")

    monkeypatch.setattr(tools, "fetch_page", blocked)
    pages = await tools.read_pages(
        ["https://x.test/a"], tools.Evidence(root="https://x.test")
    )
    assert pages[0].ok is False and pages[0].error == "blocked"

    async def proxied(url: str, **_: object):
        raise tools.EgressNotGuardedError("proxied")

    monkeypatch.setattr(tools, "fetch_page", proxied)
    with pytest.raises(tools.EgressNotGuardedError):
        await tools.read_pages(
            ["https://x.test/a"], tools.Evidence(root="https://x.test")
        )


async def test_reads_are_capped_per_call_and_per_run(monkeypatch) -> None:
    calls: List[str] = []

    async def fetch(url: str, **_: object):
        calls.append(url)
        return _result(url)

    monkeypatch.setattr(tools, "fetch_page", fetch)
    evidence = tools.Evidence(root="https://x.test")
    for batch in range(4):
        urls = [
            f"https://x.test/{batch}/{n}" for n in range(tools.MAX_READS_PER_CALL + 5)
        ]
        await tools.read_pages(urls, evidence)
    assert len(calls) == tools.MAX_READS_PER_RUN == evidence.reads


async def test_concurrent_calls_cannot_overspend_or_read_twice(monkeypatch) -> None:
    calls: Dict[str, int] = {}

    async def fetch(url: str, **_: object):
        await asyncio.sleep(0)
        calls[url] = calls.get(url, 0) + 1
        return _result(url)

    monkeypatch.setattr(tools, "fetch_page", fetch)
    evidence = tools.Evidence(root="https://x.test")
    batches = [
        [f"https://x.test/{n}" for n in range(tools.MAX_READS_PER_CALL)]
        for _ in range(3)
    ]
    await asyncio.gather(*(tools.read_pages(urls, evidence) for urls in batches))
    assert all(count == 1 for count in calls.values())
    assert evidence.reads == len(calls) == tools.MAX_READS_PER_CALL


async def test_cancelling_keeps_finished_reads_and_returns_the_rest(
    monkeypatch,
) -> None:
    slow_started = asyncio.Event()

    async def fetch(url: str, **_: object):
        if url.endswith("/slow"):
            slow_started.set()
            await asyncio.sleep(10)
        if url.endswith("/moved"):
            return _result("https://x.test/landed")
        return _result(url)

    monkeypatch.setattr(tools, "fetch_page", fetch)
    evidence = tools.Evidence(root="https://x.test")
    urls = [f"https://x.test/{n}" for n in range(4)] + [
        "https://x.test/moved",
        "https://x.test/slow",
    ]
    task = asyncio.ensure_future(tools.read_pages(urls, evidence))
    await slow_started.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert evidence.reads == 5
    # A read that redirected stays paid for, under the URL it landed on.
    assert set(evidence.pages) == set(urls[:4]) | {"https://x.test/landed"}
    assert evidence.seen("https://x.test/moved")
    assert not evidence.seen("https://x.test/slow")


def test_a_run_keeps_a_bounded_amount_of_text() -> None:
    evidence = tools.Evidence(root="https://x.test")
    big = "a" * (tools.MAX_TEXT_PER_RUN // 2 + 10)
    for n in range(3):
        evidence.add(page(f"https://x.test/{n}", big))
    assert evidence.kept_chars == tools.MAX_TEXT_PER_RUN
    assert evidence.pages["https://x.test/1"].truncated
    assert evidence.pages["https://x.test/2"].text == ""

    # A second read landing on the same URL replaces the first, not adds to it.
    evidence = tools.Evidence(root="https://x.test")
    evidence.add(page("https://x.test/a", "abc"))
    evidence.add(page("https://x.test/a", "abcd"))
    assert evidence.kept_chars == 4


# ── pulling words out ────────────────────────────────────────────────────────


async def test_find_text_is_a_plain_phrase_search() -> None:
    pages = [
        page("https://x.test/a", "Welcome. Free\n  shipping on orders over 999."),
        page("https://x.test/b", "price (a+)+$ here"),
    ]
    found = await tools.find_text("FREE   shipping", pages)
    assert [s.source_url for s in found] == ["https://x.test/a"]
    assert "Free shipping on orders over 999" in found[0].text
    # Regex syntax is only text: nothing the model writes is ever compiled.
    assert await tools.find_text("(a+)+$", pages)
    assert await tools.find_text("(?x)a{9999}", pages) == []


SHELL = """
<link href="https://x.test/_app/entry/start.AAA.js" rel="modulepreload">
<link href="/_app/entry/app.BBB.js" rel="modulepreload">
<link rel="stylesheet" href="/theme.css">
<script src="/legacy.js"></script>
<a href="/pages/contact">Contact</a><a href="mailto:hi@x.test">Mail</a>
<a href="//[::1">bad</a><img src="/logo.png"><img src="/icon@2x.png">
<style>@media(x){}</style>
a="support@x.test",b="first.last+tag@shop.example.co.in",w="https://wa.me/919876543210"
"""


async def test_page_links_sort_what_a_page_points_at() -> None:
    links = await tools.page_links(page("https://x.test/", SHELL))
    # Scripts, styles and images are not pages to read.
    assert links.pages == ["https://x.test/pages/contact"]
    assert links.emails == [
        "hi@x.test",
        "support@x.test",
        "first.last+tag@shop.example.co.in",
    ]
    assert links.phones == ["919876543210"]


@pytest.mark.parametrize(
    "text",
    [
        ("a" * 63 + "@") * (1_000_000 // 64),
        "@" * 1_000_000,
        '<a href="/p">x</a>' * 50_000,
    ],
    ids=["local-parts", "at-signs", "anchors"],
)
async def test_scans_are_cheap_on_hostile_pages(text) -> None:
    hostile = page("https://x.test/app.js", text)
    started = time.monotonic()
    await tools.page_links(hostile)
    await tools.find_text("ab", [page("https://x.test/a", "ab" * 500_000)])
    assert time.monotonic() - started < 1.5

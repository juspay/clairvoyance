"""The researcher's hands, tested on the shapes that actually broke them.

Every case here is a bug that reached a live site first. The two that matter
most are the client-routed detection (eight distinct URLs answering with one
identical shell) and relative script resolution (a bundle naming its siblings,
which must resolve against the bundle's own directory and not the site root).
"""

from typing import Dict, List

import pytest

from app.ai.voice.agents.breeze_buddy.assist.engine.research import tools


def doc(url: str, text: str = "", *, status: int = 200, size: int = 0) -> tools.Doc:
    return tools.Doc(
        url=url,
        status=status,
        content_type="text/html",
        text=text,
        size_bytes=size or len(text),
    )


# ── the tactic switch ────────────────────────────────────────────────────────


def test_identical_shells_read_as_client_routed():
    """Eight addresses, one answer. The observation the whole loop turns on."""
    shell = "<html><body></body></html>"
    docs = [doc(f"https://x.test/{name}", shell) for name in ("a", "b", "c", "d")]
    assert tools.looks_client_routed(docs) is True


def test_pages_that_differ_are_not_client_routed():
    docs = [
        doc("https://x.test/a", "<html>one</html>"),
        doc("https://x.test/b", "<html>a longer page here</html>"),
        doc("https://x.test/c", "<html>different again, longer still</html>"),
    ]
    assert tools.looks_client_routed(docs) is False


def test_two_samples_are_never_enough_to_conclude():
    """Two matching pages is a coincidence; the verdict sends 60 more fetches."""
    shell = "<html></html>"
    docs = [doc("https://x.test/a", shell), doc("https://x.test/b", shell)]
    assert tools.looks_client_routed(docs) is False


def test_failed_reads_do_not_count_toward_the_verdict():
    """Three 404s of equal size are not evidence of a client-routed app."""
    docs = [doc(f"https://x.test/{n}", "", status=404) for n in "abc"]
    assert tools.looks_client_routed(docs) is False


# ── following the thread to where the words are ──────────────────────────────

SHELL = """
<html><head>
<link href="https://x.test/_app/immutable/entry/start.AAA.js" rel="modulepreload">
<link href="/_app/immutable/entry/app.BBB.js" rel="modulepreload">
<link rel="stylesheet" href="/theme.css">
<script src="/legacy.js"></script>
</head><body>
<a href="/pages/contact">Contact</a>
<a href="mailto:hi@x.test">Mail</a>
<img src="/logo.png">
</body></html>
"""


def test_modulepreload_hrefs_are_scripts_not_pages():
    """A client-routed page declares its bundles with `href`, not `src`.

    Filing those under pages loses the only thread that leads to the site's
    words — which is exactly what happened on the first live run.
    """
    found = tools.trails(doc("https://x.test/", SHELL))
    assert "https://x.test/_app/immutable/entry/start.AAA.js" in found.scripts
    assert "https://x.test/_app/immutable/entry/app.BBB.js" in found.scripts
    assert "https://x.test/legacy.js" in found.scripts
    assert not any(url.endswith(".js") for url in found.pages)


def test_wordless_assets_are_not_offered_as_pages():
    found = tools.trails(doc("https://x.test/", SHELL))
    assert "https://x.test/pages/contact" in found.pages
    assert not any(url.endswith((".css", ".png")) for url in found.pages)


def test_a_bundle_names_its_siblings_relative_to_itself():
    """Resolving these against the site root turns 60 real files into 60 404s."""
    bundle = doc(
        "https://x.test/_app/immutable/entry/start.AAA.js",
        'import("../chunks/CW3.js");const b="../nodes/12.js"',
    )
    found = tools.trails(bundle)
    assert "https://x.test/_app/immutable/chunks/CW3.js" in found.scripts
    assert "https://x.test/_app/immutable/nodes/12.js" in found.scripts


def test_contacts_come_out_of_anything_including_a_bundle():
    bundle = doc(
        "https://x.test/_app/immutable/nodes/3.js",
        'a="support@x.test",b="hello@x.test",w="https://wa.me/919876543210"',
    )
    found = tools.trails(bundle)
    assert found.emails == ["support@x.test", "hello@x.test"]
    assert found.phones == ["919876543210"]


# ── mining ───────────────────────────────────────────────────────────────────


def test_mine_returns_group_one_with_its_source():
    docs = [doc("https://x.test/a", 'name="Breeze" name="Buddy"')]
    hits = tools.mine(r'name="([^"]+)"', docs)
    assert [hit.value for hit in hits] == ["Breeze", "Buddy"]
    assert hits[0].source_url == "https://x.test/a"


def test_mine_keeps_the_first_sighting_of_a_repeated_value():
    docs = [
        doc("https://x.test/a", "hello@x.test"),
        doc("https://x.test/b", "hello@x.test"),
    ]
    hits = tools.mine(r"[a-z]+@[a-z.]+", docs)
    assert len(hits) == 1
    assert hits[0].source_url == "https://x.test/a"


def test_a_bad_pattern_returns_nothing_rather_than_exploding():
    assert tools.mine(r"(unclosed", [doc("https://x.test/a", "text")]) == []


def test_copy_survives_the_filter_and_machinery_does_not():
    """The `%&` in this very character class once crashed the pattern build."""
    text = (
        '"Breeze Automatic cuts through your business data to serve you '
        'real-time insights, tailored fixes and instant action." '
        '"toolbar-right-content merchant-view" '
        '"position: fixed; bottom: 14px; z-index: 99999" '
        '"M14.16 4.918C11.23 7.048 8.6 9.548 6.35 12.348" '
        '"Save 20% on orders over 2,000 & get free delivery"'
    )
    kept = [hit.value for hit in tools.readable_lines([doc("https://x.test/a", text)])]
    assert any(value.startswith("Breeze Automatic cuts") for value in kept)
    assert any("Save 20%" in value for value in kept)
    assert not any("toolbar-right-content" in value for value in kept)
    assert not any("z-index" in value for value in kept)
    assert not any(value.startswith("M14.16") for value in kept)


# ── the fetch policy ─────────────────────────────────────────────────────────


def test_site_of_ignores_subdomains_and_scheme():
    assert tools.site_of("https://www.milton.in/x") == "milton.in"
    assert tools.site_of("checkout.example.co/y") == "example.co"
    assert tools.same_site("https://cdn.x.test/a.js", "https://x.test") is True
    assert tools.same_site("https://evil.test/a.js", "https://x.test") is False


@pytest.mark.asyncio
async def test_gather_refuses_to_wander_off_the_brands_own_site(monkeypatch):
    seen: List[str] = []

    async def fake_fetch(url: str, **_: object):
        seen.append(url)
        raise tools.FetchFailedError("nope")

    monkeypatch.setattr(tools, "fetch_page", fake_fetch)
    evidence = tools.Evidence(root="https://x.test")
    await tools.gather(["https://x.test/a", "https://elsewhere.test/b"], evidence)
    assert seen == ["https://x.test/a"]
    assert any("skipped off-site" in step for step in evidence.steps)


@pytest.mark.asyncio
async def test_a_failed_read_becomes_a_document_not_an_exception(monkeypatch):
    """A researcher learns from a 404; it must never end the run."""

    async def fake_fetch(url: str, **_: object):
        raise tools.UnsafeUrlError("blocked")

    monkeypatch.setattr(tools, "fetch_page", fake_fetch)
    evidence = tools.Evidence(root="https://x.test")
    docs = await tools.gather(["https://x.test/a"], evidence)
    assert len(docs) == 1 and docs[0].ok is False
    assert docs[0].error == "blocked"


@pytest.mark.asyncio
async def test_unguarded_egress_stops_the_run_instead_of_failing_eighty_times(
    monkeypatch,
):
    async def fake_fetch(url: str, **_: object):
        raise tools.EgressNotGuardedError("proxied")

    monkeypatch.setattr(tools, "fetch_page", fake_fetch)
    evidence = tools.Evidence(root="https://x.test")
    with pytest.raises(tools.EgressNotGuardedError):
        await tools.gather(["https://x.test/a"], evidence)


@pytest.mark.asyncio
async def test_gather_never_reads_the_same_url_twice(monkeypatch):
    calls: Dict[str, int] = {}

    async def fake_fetch(url: str, **_: object):
        calls[url] = calls.get(url, 0) + 1
        raise tools.FetchFailedError("nope")

    monkeypatch.setattr(tools, "fetch_page", fake_fetch)
    evidence = tools.Evidence(root="https://x.test")
    await tools.gather(["https://x.test/a", "https://x.test/a"], evidence)
    await tools.gather(["https://x.test/a"], evidence)
    assert calls == {"https://x.test/a": 1}


# ── the evidence book ────────────────────────────────────────────────────────


def test_a_note_without_a_value_is_not_a_note():
    evidence = tools.Evidence(root="https://x.test")
    evidence.note("tagline", "   ", "https://x.test/")
    evidence.note("tagline", "The Commerce Super Stack", "https://x.test/")
    assert len(evidence.notes) == 1
    assert evidence.notes[0].source_url == "https://x.test/"
    assert evidence.notes[0].on_site is True

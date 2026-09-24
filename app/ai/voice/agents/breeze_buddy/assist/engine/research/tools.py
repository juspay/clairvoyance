"""The researcher's tools: read a merchant's pages and pull facts out of them.

``read_pages`` fetches, ``page_links`` finds where to go next, ``find_text`` and
``readable_lines`` pull out words, and ``Evidence`` records every page read and
every fact noted with the page it came from.

Page text is attacker-controlled and the model chooses what to read, so reads
stay on the merchant's own host (every redirect hop is checked before it is
sent) and within a budget, the model searches by plain phrase rather than
regex, and CPU-bound helpers run off the event loop.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Sequence, Set
from urllib.parse import urljoin, urlsplit

import regex

from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    FetchFailedError,
    FetchResult,
    UnsafeUrlError,
    fetch_page,
)
from app.core.logger import logger

# Heavy storefront pages run to a few MB; 1 MB cut off real footers.
MAX_PAGE_BYTES = 4 * 1024 * 1024
READ_TIMEOUT_SECONDS = 20.0
MAX_PARALLEL_READS = 8
MAX_READS_PER_CALL = 30
MAX_READS_PER_RUN = 60
# Characters of page text one run keeps; a str can take 4 bytes per character.
MAX_TEXT_PER_RUN = 32 * 1024 * 1024
PATTERN_TIMEOUT_SECONDS = 1.0
MAX_PHRASE_LENGTH = 200
MAX_PHRASE_OCCURRENCES = 5000

_PASSAGE_CHARS = 160
_MAX_AT_SIGNS = 2000
_MAX_LINKS_PER_PAGE = 2000
_EMAIL_LOCAL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-"
)
# "icon@2x.png" looks like an address; these endings are files, not domains.
_FILE_ENDINGS = frozenset(
    ("png", "jpg", "jpeg", "webp", "avif", "gif", "svg", "ico", "js", "mjs", "css")
)
_TEXT_CONTENT_TYPES = ("text/", "javascript", "json", "xml", "ecmascript")
_NOT_TEXT_SUFFIXES = (
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".mp4",
    ".webm",
    ".pdf",
    ".zip",
)

# On ``regex`` rather than ``re`` so matching a 4 MB page releases the GIL.
_HREF = regex.compile(r"""href\s*=\s*["']([^"'>\s]+)["']""", regex.IGNORECASE)
_SRC = regex.compile(r"""src\s*=\s*["']([^"'>\s]+)["']""", regex.IGNORECASE)
# Bounded: unbounded, a quote before 4 MB of "a/a/a/..." backtracks for minutes.
_MODULE = regex.compile(
    r"""["'`]([A-Za-z0-9_./-]{0,256}/[A-Za-z0-9_.-]{1,128}\.(?:js|mjs))["'`]"""
)
_TEL = regex.compile(r"(?:tel:|wa\.me/)\+?([0-9][0-9 ()-]{6,17})", regex.IGNORECASE)
# Matched outward from each "@": trying an email pattern at every position is
# quadratic on hostile text.
_EMAIL_LOCAL = re.compile(r"[A-Za-z0-9._%+-]{1,64}\Z")
_EMAIL_DOMAIN = re.compile(
    r"[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,6}\.[A-Za-z]{2,24}"
)
_QUOTED_LINE = regex.compile(
    "[\"'`]([A-Za-z][A-Za-z0-9 ,.:;%&/()'’!?—-]{30,220})[\"'`]"
)
# Code, CSS and class names rather than something a person wrote.
_MACHINERY = re.compile(
    r"(^[Mm][0-9]|function|prototype|undefined|webpack|svelte-|"
    r"[a-z]+-[a-z]+-[a-z]+|;|\{|\.js$|^[a-z-]+$|px |rgba?\()",
)


@dataclass
class Page:
    """One URL that was read — a page or a script file — and how it came back."""

    url: str
    status: int
    content_type: str
    text: str
    size_bytes: int
    truncated: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.status < 400 and bool(self.text)


@dataclass
class Snippet:
    """Text found on a page, and the page it was found on."""

    text: str
    source_url: str


@dataclass
class Note:
    """A fact the researcher recorded, and the page that said it."""

    field_name: str
    value: str
    source_url: str
    noted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PageLinks:
    """Where a page points: other pages, script files, emails and phones."""

    pages: List[str] = field(default_factory=list)
    scripts: List[str] = field(default_factory=list)
    emails: List[str] = field(default_factory=list)
    phones: List[str] = field(default_factory=list)


@dataclass
class Evidence:
    """One research run: the pages read, the facts noted, the reads spent."""

    root: str
    pages: Dict[str, Page] = field(default_factory=dict)
    notes: List[Note] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    reads: int = 0
    kept_chars: int = 0
    # Every URL asked for or landed on, so a redirect is not read twice.
    requested: Set[str] = field(default_factory=set)

    def add(self, page: Page) -> None:
        room = max(0, MAX_TEXT_PER_RUN - self.kept_chars)
        if len(page.text) > room:
            page.text, page.truncated = page.text[:room], True
        self.kept_chars += len(page.text)
        self.pages[page.url] = page
        self.requested.add(page.url)

    def note(self, field_name: str, value: str, source_url: str) -> None:
        text = (value or "").strip()
        if text:
            self.notes.append(
                Note(field_name=field_name, value=text, source_url=source_url)
            )

    def step(self, description: str) -> None:
        self.steps.append(description)

    def readable(self) -> List[Page]:
        return [page for page in self.pages.values() if page.ok]

    def seen(self, url: str) -> bool:
        return url in self.requested

    def _claim(self, url: str) -> None:
        self.requested.add(url)
        self.reads += 1

    def _release(self, url: str) -> None:
        # A page another read already landed on stays read.
        if url in self.requested and url not in self.pages:
            self.requested.discard(url)
            self.reads -= 1


def site_of(url: str) -> str:
    """The URL's host, lowercased and without ``www.``; "" if it cannot parse."""
    try:
        host = urlsplit(url if "//" in url else f"https://{url}").hostname or ""
    except ValueError:
        return ""
    host = host.lower().strip(".")
    if not host.isascii():
        # Fetched URLs carry the ASCII (xn--) form; compare like with like.
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return ""
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, root: str) -> bool:
    """Whether ``url`` is on ``root``'s host or a subdomain of it.

    The whole host is compared: matching only the last two labels would make
    every store on a shared hosting domain one site.
    """
    host, base = site_of(url), site_of(root)
    return bool(host and base) and (host == base or host.endswith("." + base))


async def read_pages(urls: Sequence[str], evidence: Evidence) -> List[Page]:
    """Read on-site URLs not read before, within the run's budget.

    A failed read comes back as a ``Page`` with ``error`` set, never raised;
    only ``EgressNotGuardedError`` propagates, since every read would fail.
    """
    budget = min(MAX_READS_PER_CALL, MAX_READS_PER_RUN - evidence.reads)
    if budget <= 0:
        evidence.step("read budget spent")
        return []

    wanted: List[str] = []
    for raw in urls:
        url = (raw or "").strip()
        if not url or evidence.seen(url) or url in wanted:
            continue
        if not same_site(url, evidence.root):
            evidence.step(f"skipped off-site {url}")
            continue
        wanted.append(url)
        if len(wanted) >= budget:
            break
    if not wanted:
        return []

    # Claimed before the first await, so concurrent calls cannot overspend.
    for url in wanted:
        evidence._claim(url)

    gate = asyncio.Semaphore(MAX_PARALLEL_READS)

    async def read_one(url: str) -> Page:
        async with gate:
            page = await _read(url, evidence.root)
            # Kept as it lands, so the run's text cap bounds memory too:
            # a finished page is trimmed now, not after the whole batch.
            evidence.add(page)
            return page

    tasks = [asyncio.ensure_future(read_one(url)) for url in wanted]
    try:
        pages = await asyncio.gather(*tasks)
    except BaseException:
        # Finished reads are already kept; stop and hand back the rest.
        for url, task in zip(wanted, tasks):
            if not (task.done() and not task.cancelled() and task.exception() is None):
                task.cancel()
                evidence._release(url)
        raise

    usable = sum(1 for page in pages if page.ok)
    evidence.step(f"read {len(pages)} url(s), {usable} usable")
    return list(pages)


async def _read(url: str, root: str) -> Page:
    try:
        result = await fetch_page(
            url,
            max_bytes=MAX_PAGE_BYTES,
            timeout_seconds=READ_TIMEOUT_SECONDS,
            allow_url=lambda hop: same_site(hop, root),
        )
    except EgressNotGuardedError:
        raise
    except (UnsafeUrlError, FetchFailedError) as exc:
        return _failed(url, str(exc))
    except Exception as exc:
        logger.info(f"assist research: {url} unreadable ({exc})")
        return _failed(url, "unreadable")
    page = _as_page(url, result)
    if not same_site(page.url, root):
        return _failed(url, "redirected off-site", status=page.status)
    return page


def _failed(url: str, error: str, *, status: int = 0) -> Page:
    return Page(
        url=url, status=status, content_type="", text="", size_bytes=0, error=error
    )


def _as_page(url: str, result: FetchResult) -> Page:
    content_type = (result.headers.get("content-type") or "").lower()
    is_text = not content_type or any(
        marker in content_type for marker in _TEXT_CONTENT_TYPES
    )
    return Page(
        url=result.final_url or url,
        status=result.status,
        content_type=content_type,
        text=result.body if is_text else "",
        size_bytes=result.size_bytes,
        truncated=result.truncated,
    )


async def find_text(
    phrase: str, pages: Iterable[Page], *, limit: int = 60
) -> List[Snippet]:
    """Every passage containing ``phrase``, case- and whitespace-insensitive."""
    return await asyncio.to_thread(_find_text, phrase, list(pages), limit)


def _find_text(phrase: str, pages: List[Page], limit: int) -> List[Snippet]:
    needle = " ".join((phrase or "").split()).lower()
    if not 2 <= len(needle) <= MAX_PHRASE_LENGTH:
        return []
    found: List[Snippet] = []
    seen: Set[str] = set()
    occurrences = 0
    for page in pages:
        flat = " ".join(page.text.split())
        haystack = flat.lower()
        # Lowercasing can change a string's length; offsets then only hold
        # for the lowered copy.
        source = flat if len(haystack) == len(flat) else haystack
        at = haystack.find(needle)
        while at != -1:
            occurrences += 1
            if occurrences > MAX_PHRASE_OCCURRENCES:
                return found
            start = max(0, at - _PASSAGE_CHARS)
            passage = source[start : at + len(needle) + _PASSAGE_CHARS].strip()
            if passage not in seen:
                seen.add(passage)
                found.append(Snippet(text=passage, source_url=page.url))
                if len(found) >= limit:
                    return found
            at = haystack.find(needle, at + len(needle))
    return found


async def page_links(page: Page) -> PageLinks:
    """Links, script files, emails and phone numbers on ``page``."""
    return await asyncio.to_thread(_page_links, page)


def _page_links(page: Page) -> PageLinks:
    links = PageLinks()
    if not page.text:
        return links
    # A script can arrive as href (modulepreload) or be named inside another
    # script, so it is sorted by what the address is, not how it was declared.
    raw_links = [
        raw
        for pattern in (_HREF, _SRC, _MODULE)
        for raw in _first_matches(pattern, page.text, _MAX_LINKS_PER_PAGE)
    ]
    for raw in raw_links:
        if raw.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        try:
            # Relative to the page itself: bundles name their siblings relatively.
            target = urljoin(page.url, raw)
            path = urlsplit(target).path.lower()
        except ValueError:
            continue
        if path.endswith((".js", ".mjs")):
            links.scripts.append(target)
        elif not path.endswith(_NOT_TEXT_SUFFIXES):
            links.pages.append(target)
    links.pages = list(dict.fromkeys(links.pages))[:_MAX_LINKS_PER_PAGE]
    links.scripts = list(dict.fromkeys(links.scripts))[:_MAX_LINKS_PER_PAGE]
    links.emails = _emails(page.text)
    links.phones = list(
        dict.fromkeys(_first_matches(_TEL, page.text, _MAX_LINKS_PER_PAGE))
    )
    return links


def _first_matches(pattern: "regex.Pattern[str]", text: str, limit: int) -> List[str]:
    found: List[str] = []
    try:
        for match in pattern.finditer(
            text, concurrent=True, timeout=PATTERN_TIMEOUT_SECONDS
        ):
            found.append(match.group(1))
            if len(found) >= limit:
                break
    except TimeoutError:
        logger.info("assist research: link pattern timed out")
    return found


def _emails(text: str) -> List[str]:
    found: List[str] = []
    at = text.find("@")
    checked = 0
    while at != -1 and checked < _MAX_AT_SIGNS:
        # CSS (@media) and JS (@import) put "@" after a space or brace; only
        # an "@" that could end a local part counts towards the cap.
        if at > 0 and text[at - 1] in _EMAIL_LOCAL_CHARS:
            checked += 1
            local = _EMAIL_LOCAL.search(text, max(0, at - 64), at)
            domain = _EMAIL_DOMAIN.match(text, at + 1)
            if (
                local
                and domain
                and domain.group(0).rsplit(".", 1)[-1].lower() not in _FILE_ENDINGS
            ):
                found.append(f"{local.group(0)}@{domain.group(0)}")
        at = text.find("@", at + 1)
    return list(dict.fromkeys(found))


async def readable_lines(pages: Iterable[Page], *, limit: int = 400) -> List[Snippet]:
    """Quoted sentences a person wrote, from markup or script files."""
    return await asyncio.to_thread(_readable_lines, list(pages), limit)


def _readable_lines(pages: List[Page], limit: int) -> List[Snippet]:
    # Most quoted strings in a bundle are code; match wide, keep the prose.
    lines: List[Snippet] = []
    for snippet in _match_all(_QUOTED_LINE, pages, limit=limit * 20):
        if _MACHINERY.search(snippet.text) or len(snippet.text.split()) < 3:
            continue
        lines.append(snippet)
        if len(lines) >= limit:
            break
    return lines


def _match_all(
    pattern: "regex.Pattern[str]", pages: List[Page], *, limit: int
) -> List[Snippet]:
    """Distinct matches of one of this module's patterns, under one deadline."""
    deadline = time.monotonic() + PATTERN_TIMEOUT_SECONDS
    found: List[Snippet] = []
    seen: Set[str] = set()
    for page in pages:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            for match in pattern.finditer(
                page.text, concurrent=True, timeout=remaining
            ):
                value = (match.group(1) or "").strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                found.append(Snippet(text=value, source_url=page.url))
                if len(found) >= limit:
                    return found
        except TimeoutError:
            logger.info("assist research: pattern timed out")
            break
    return found


def looks_client_routed(pages: Sequence[Page]) -> bool:
    """Whether 3+ readable pages came back identical in size and status.

    That is a server answering every address with one shell; the words are in
    its script files instead. Truncated pages all share the cap as their size,
    so they say nothing either way.
    """
    readable = [page for page in pages if page.ok and not page.truncated]
    return (
        len(readable) >= 3
        and len({(page.size_bytes, page.status) for page in readable}) == 1
    )


__all__ = [
    "Evidence",
    "MAX_PAGE_BYTES",
    "MAX_PARALLEL_READS",
    "MAX_PHRASE_LENGTH",
    "MAX_PHRASE_OCCURRENCES",
    "MAX_READS_PER_CALL",
    "MAX_READS_PER_RUN",
    "MAX_TEXT_PER_RUN",
    "READ_TIMEOUT_SECONDS",
    "Note",
    "Page",
    "PageLinks",
    "Snippet",
    "find_text",
    "looks_client_routed",
    "page_links",
    "read_pages",
    "readable_lines",
    "same_site",
    "site_of",
]
